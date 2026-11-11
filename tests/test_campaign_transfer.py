"""Archived startup ownership is earned once, never inferred from an old PID."""

import copy
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_campaign_adoption
from conftest import git
from test_campaign_paradev import Host as WorkspaceHost

from bmad_loop import campaign_paradev
from bmad_loop import campaign_transfer as transfer
from bmad_loop.campaign_database import MARKER, TRANSFER_MARKER, CampaignDatabase
from bmad_loop.campaign_model import CampaignError, CampaignUnit, ParadevSpec, UnitState
from bmad_loop.campaign_native_inventory import NativeServiceObserver
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_store import CampaignStore, RevisionConflict, add_action

adoption_case = test_campaign_adoption.case


class Host(WorkspaceHost):
    def rpc(self, procedure, payload=None, mutate=False):
        if procedure == "workspace.get":
            return next(copy.deepcopy(row) for row in self.workspaces if row["id"] == payload["id"])
        if procedure == "terminal.list":
            return {"sessions": []}
        return super().rpc(procedure, payload, mutate=mutate)

    def cli(self, args):
        if args[:2] == ["workspaces", "get"]:
            return next(copy.deepcopy(row) for row in self.workspaces if row["id"] == args[2])
        if args[:2] == ["automations", "list"]:
            return []
        return super().cli(args)


@pytest.fixture
def case(adoption_case, tmp_path, monkeypatch):
    """Use ordinary native preparation/apply, then archive only its sandbox checkout."""
    fixture = adoption_case
    # The reused source fixture's unrelated legacy checkout is not transferred.
    git(fixture.repo, "worktree", "remove", str(fixture.workspace))
    host = Host(fixture.repo, fixture.root)
    monkeypatch.setattr(campaign_paradev, "Host", lambda: host)
    store = CampaignStore(tmp_path / "transfer-state", clock=lambda: 100)
    snapshot = fixture.snapshot
    snapshot.phase = "stopped"
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        paradev=ParadevSpec(reflect=True, project_id="project", worktree_root=str(fixture.root)),
    )
    unit = snapshot.units[fixture.unit.id]
    unit.state = UnitState.PLANNING
    snapshot = store.create(snapshot)
    runtime = CampaignRuntime(fixture.repo, store, gateway=fixture.gateway)
    action = add_action(snapshot, "prepare-unit", unit.id, {})
    snapshot = store.save(snapshot, snapshot.revision)
    action = next(row for row in snapshot.outbox if row.id == action.id)
    observed = runtime._perform_prepare_unit(snapshot, action)
    assert observed.state == "confirmed", observed.reason
    runtime.apply(snapshot, action, observed)
    action.state, action.receipt = "confirmed", observed.data
    snapshot = store.save(snapshot, snapshot.revision)
    binding = snapshot.units[unit.id].bindings
    workspace = Path(binding["workspace"])
    assert not git(workspace, "status", "--porcelain")
    git(workspace, "push", "origin", binding["branch"])
    git(fixture.repo, "worktree", "remove", str(workspace))
    host.workspaces[0].update(
        archivedAt="archive-v1",
        archiveReason="deleted",
        worktreeExists=False,
        headSha=binding["head"],
    )
    fixture.pr["head"].update(ref=binding["branch"], sha=binding["head"])
    successor = dataclasses.replace(
        fixture.snapshot,
        id="successor",
        revision=0,
        units={unit.id: CampaignUnit(unit.spec)},
        phase="stopped",
        bindings={},
        outbox=[],
        events=[],
    )
    successor = store.create(successor)
    request = transfer.TransferRequest(
        snapshot.id,
        unit.id,
        successor.id,
        unit.id,
        7,
        snapshot.revision,
        successor.revision,
        "Retain this archived startup branch",
    )
    return SimpleNamespace(
        **vars(fixture),
        store=store,
        before=snapshot,
        successor=successor,
        request=request,
        host=host,
        observer=NativeServiceObserver(
            "project",
            host=host,
            archived_workspaces=(binding["native"]["workspace_id"],),
            clock=lambda: 100,
        ),
    )


def invoke(case, *, preview=False, request=None):
    return (transfer.preview if preview else transfer.release)(
        case.store,
        request or case.request,
        project=case.repo,
        gateway=case.gateway,
        observer=case.observer,
        now=100,
    )


def test_real_preparation_preview_release_and_lost_reply_preserve_authority(case):
    before = case.store.load(case.before.id)
    initial = (case.store.directory(before.id) / "snapshot.json").read_bytes()
    shown = invoke(case, preview=True)
    assert shown["preview"] and shown["operation"].startswith("startup-only")
    assert shown["unmapped_units"] == []
    assert (case.store.directory(before.id) / "snapshot.json").read_bytes() == initial
    assert case.store.load(before.id).revision == before.revision
    result = invoke(case)
    after = case.store.load(before.id)
    assert result["record"] == transfer.read_release(case.store, before.id, case.unit.id)
    assert after.phase == "stopped" and after.units == before.units
    assert (
        after.config_snapshot == before.config_snapshot
        and after.config_revision == before.config_revision
    )
    assert after.outbox == before.outbox and case.store.load("successor") == case.successor
    assert transfer.retired(after) and transfer.released(after, case.unit.id)
    assert (
        json.loads((case.store.directory(before.id) / "snapshot.json").read_text())
        == TRANSFER_MARKER
    )
    with CampaignDatabase(case.store.directory(before.id)).connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
    assert invoke(case)["idempotent"]
    assert case.store.load(before.id).revision == after.revision
    assert not Path(after.units[case.unit.id].bindings["workspace"]).exists()


@pytest.mark.parametrize(
    "fault",
    [
        "phase",
        "unit-attempt",
        "pending-write",
        "uncertain-read",
        "successor-active",
        "recipient-binding",
        "pr",
        "head",
        "source",
    ],
)
def test_startup_transfer_refuses_unearned_or_changed_authority(case, fault):
    snapshot = case.store.load(case.before.id)
    if fault == "phase":
        snapshot.phase = "active"
    elif fault == "unit-attempt":
        snapshot.units[case.unit.id].attempt = 1
    elif fault == "pending-write":
        add_action(snapshot, "prepare-unit", case.unit.id, {})
    elif fault == "uncertain-read":
        add_action(snapshot, "inventory", "", {}).state = "sent_unknown"
    elif fault in {"successor-active", "recipient-binding"}:
        recipient = case.store.load("successor")
        if fault == "successor-active":
            recipient.phase = "active"
        else:
            recipient.units[case.unit.id].bindings["branch"] = "other"
        recipient = case.store.save(recipient, recipient.revision)
        case.request = dataclasses.replace(
            case.request, expected_successor_revision=recipient.revision
        )
    elif fault == "pr":
        case.pr["state"] = "closed"
    elif fault == "head":
        case.pr["head"]["sha"] = "a" * 40
    elif fault == "source":
        recipient = case.store.load("successor")
        recipient.units[case.unit.id].spec = dataclasses.replace(
            recipient.units[case.unit.id].spec, scopes=("other",)
        )
        recipient.graph_revision += 1
        recipient = case.store.save(recipient, recipient.revision)
        case.request = dataclasses.replace(
            case.request, expected_successor_revision=recipient.revision
        )
    if fault in {"phase", "unit-attempt", "pending-write", "uncertain-read"}:
        snapshot = case.store.save(snapshot, snapshot.revision)
        case.request = dataclasses.replace(case.request, expected_revision=snapshot.revision)
    with pytest.raises(CampaignError):
        invoke(case)
    assert not transfer.retired(case.store.load(snapshot.id))


@pytest.mark.parametrize(
    "fault", ["resume", "reconfigure", "new-effect", "unit", "remove", "rewrite"]
)
def test_ordinary_save_cannot_reopen_retired_predecessor(case, fault):
    invoke(case)
    snapshot = case.store.load(case.before.id)
    if fault == "resume":
        snapshot.phase = "active"
    elif fault == "reconfigure":
        snapshot.config_revision += 1
    elif fault == "new-effect":
        add_action(snapshot, "launch", case.unit.id, {})
    elif fault == "unit":
        snapshot.units[case.unit.id].attempt = 1
    elif fault == "remove":
        snapshot.bindings.pop("ownership_retirement")
    else:
        snapshot.bindings["ownership_transfers"][case.unit.id]["successor"] = "intruder"
    with pytest.raises(CampaignError, match="retire|immutable"):
        case.store.save(snapshot, snapshot.revision)
    with pytest.raises(CampaignError, match="retired"):
        transfer.require_not_released(case.store.load(case.before.id), "unmapped-intake", "launch")


def test_ordinary_save_and_create_cannot_forge_a_release(case):
    record = invoke(case, preview=True)["record"]
    snapshot = case.store.load(case.before.id)
    snapshot.bindings["ownership_transfers"] = {case.unit.id: record}
    with pytest.raises(CampaignError, match="explicit transfer"):
        case.store.save(snapshot, snapshot.revision)
    snapshot.id = "forged"
    with pytest.raises(CampaignError, match="explicit transfer"):
        case.store.create(snapshot)


def test_atomic_schema_fence_survives_crash_before_marker_and_never_downgrades(case, monkeypatch):
    original = case.store._write

    def fail_marker(path, value):
        if path.name == "snapshot.json" and value == TRANSFER_MARKER:
            raise OSError("lost reply before marker")
        return original(path, value)

    monkeypatch.setattr(case.store, "_write", fail_marker)
    with pytest.raises(OSError, match="lost reply"):
        invoke(case)
    path = case.store.directory(case.before.id) / "snapshot.json"
    assert json.loads(path.read_text()) == MARKER
    # DB4 is authoritative even with marker3, and the old reader's accepted set
    # {2,3} refuses it before reading an ignored retirement binding.
    database = CampaignDatabase(path.parent)
    with database.connect() as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == 4 and version not in {2, 3}
    after = case.store.load(case.before.id)
    assert transfer.retired(after)
    monkeypatch.setattr(case.store, "_write", original)
    assert invoke(case)["idempotent"]
    after = case.store.save(after, after.revision)
    assert json.loads(path.read_text()) == TRANSFER_MARKER
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4


def test_successor_cas_and_release_retry_cannot_change_recipient(case, monkeypatch):
    original = case.observer

    def race(*args):
        result = original(*args)
        successor = case.store.load("successor")
        successor.bindings["changed"] = True
        case.store.save(successor, successor.revision)
        return result

    case.observer = race
    with pytest.raises(RevisionConflict):
        invoke(case)
    assert not transfer.retired(case.store.load(case.before.id))


def test_legacy_json_requires_explicit_migration_and_unrelated_store_stays_v3(case, tmp_path):
    legacy = CampaignStore(tmp_path / "legacy", storage="json")
    legacy.create(dataclasses.replace(case.before, events=list(case.before.events)))
    legacy.create(dataclasses.replace(case.successor, events=list(case.successor.events)))
    with pytest.raises(CampaignError, match="SQLite state migration"):
        transfer.preview(
            legacy,
            case.request,
            project=case.repo,
            gateway=case.gateway,
            observer=case.observer,
            now=100,
        )
    invoke(case)
    with CampaignDatabase(case.store.directory("successor")).connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3


def test_default_freshness_clock_is_sampled_after_native_observation(case, monkeypatch):
    ticks = iter((99, 100, 100, 100))
    monkeypatch.setattr(transfer.time, "time", lambda: next(ticks))
    # The observer itself consumes time; a timestamp captured before it would be
    # behind its freshly completed observation and would reject healthy startup.
    original = case.observer

    def observe(*args):
        transfer.time.time()
        return original(*args)

    result = transfer.preview(
        case.store, case.request, project=case.repo, gateway=case.gateway, observer=observe
    )
    assert result["record"]["ownership_observation"]["observed_at"] == 100


def test_transfer_transaction_failure_does_not_publish_retirement_or_schema4(case, monkeypatch):
    def fail_append(*args):
        raise RuntimeError("transaction interrupted")

    monkeypatch.setattr(CampaignDatabase, "_append", fail_append)
    with pytest.raises(RuntimeError, match="transaction interrupted"):
        invoke(case)
    state = case.store.load(case.before.id)
    assert not transfer.retired(state) and state.revision == case.before.revision
    with CampaignDatabase(case.store.directory(state.id)).connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3


def test_old_database_reader_rejects_committed_retirement_even_before_marker(case, monkeypatch):
    from bmad_loop import campaign_database

    invoke(case)
    case.store._write(case.store.directory(case.before.id) / "snapshot.json", MARKER)
    # The old reader accepted {2,3}; emulate that exact reader boundary, not a
    # marker-only check which could still admit the old writer after a crash.
    monkeypatch.setattr(campaign_database, "TRANSFER_SCHEMA_VERSION", 3)
    with pytest.raises(CampaignError, match="unsupported campaign database schema"):
        CampaignDatabase(case.store.directory(case.before.id)).read()


def test_idempotent_release_cannot_retarget_or_redefine_owner_intent(case):
    result = invoke(case)
    for change in (
        {"successor": "intruder"},
        {"successor_unit": "other"},
        {"pr_number": 8},
        {"reason": "different decision"},
    ):
        with pytest.raises(CampaignError, match="immutable prior intent"):
            invoke(case, request=dataclasses.replace(case.request, **change))
    assert transfer.read_release(case.store, case.before.id, case.unit.id) == result["record"]


def test_stale_native_census_is_not_ownership_release(case):
    observed = case.observer(case.repo, Path(case.before.common_dir))
    case.observer = dataclasses.replace(observed, observed_at=-21)
    with pytest.raises(CampaignError, match="stale"):
        invoke(case)
    assert not transfer.retired(case.store.load(case.before.id))


def test_additional_release_keeps_first_fence_and_same_named_successor(case, monkeypatch):
    """Two ordinary archived allocations transfer without reviving old admissions."""
    snapshot = case.store.load(case.before.id)
    story = case.repo / "specs/stories/other-story.md"
    story.write_text("---\nstatus: ready-for-dev\n---\n# Other\nOther intent.\n", encoding="utf-8")
    board = case.repo / "specs/stories.yaml"
    board.write_text(
        board.read_text() + "- id: other\n  title: Other\n  description: Other intent.\n",
        encoding="utf-8",
    )
    git(case.repo, "add", "specs")
    git(case.repo, "commit", "-qm", "author another selected source")
    git(case.repo, "push", "origin", "main")
    case.pr["base"]["sha"] = git(case.repo, "rev-parse", "HEAD")
    second_spec = dataclasses.replace(
        case.unit, id="source:project:other", selector="other", title="Other"
    )
    snapshot.units[second_spec.id] = CampaignUnit(second_spec, state=UnitState.PLANNING)
    snapshot.graph_revision += 1
    action = add_action(snapshot, "prepare-unit", second_spec.id, {})
    snapshot = case.store.save(snapshot, snapshot.revision)
    action = next(row for row in snapshot.outbox if row.id == action.id)
    runtime = CampaignRuntime(case.repo, case.store, gateway=case.gateway)
    observed = runtime._perform_prepare_unit(snapshot, action)
    assert observed.state == "confirmed", observed.reason
    runtime.apply(snapshot, action, observed)
    action.state, action.receipt = "confirmed", observed.data
    snapshot = case.store.save(snapshot, snapshot.revision)
    binding = snapshot.units[second_spec.id].bindings
    workspace = Path(binding["workspace"])
    assert not git(workspace, "status", "--porcelain")
    git(workspace, "push", "origin", binding["branch"])
    git(case.repo, "worktree", "remove", str(workspace))
    case.host.workspaces[1].update(
        archivedAt="archive-v2",
        archiveReason="deleted",
        worktreeExists=False,
        headSha=binding["head"],
    )
    recipient = case.store.load("successor")
    recipient.units[second_spec.id] = CampaignUnit(second_spec)
    recipient.graph_revision += 1
    recipient = case.store.save(recipient, recipient.revision)
    case.request = dataclasses.replace(
        case.request,
        expected_revision=snapshot.revision,
        expected_successor_revision=recipient.revision,
    )
    case.observer = NativeServiceObserver(
        "project",
        host=case.host,
        archived_workspaces=tuple(row["id"] for row in case.host.workspaces),
        clock=lambda: 100,
    )
    from bmad_loop import campaign_native_inventory

    calls = []

    def native_default(project_id, *, archived_workspaces):
        calls.append(tuple(archived_workspaces))
        return NativeServiceObserver(
            project_id, host=case.host, archived_workspaces=archived_workspaces, clock=lambda: 100
        )

    monkeypatch.setattr(campaign_native_inventory, "NativeServiceObserver", native_default)
    case.observer = None
    first = invoke(case)
    assert set(calls[-1]) == {row["id"] for row in case.host.workspaces}
    retired_before = case.store.load(snapshot.id)
    case.pr.update(number=8, node_id="PR-8")
    case.pr["head"].update(ref=binding["branch"], sha=binding["head"])
    second = dataclasses.replace(
        case.request,
        unit=second_spec.id,
        successor_unit=second_spec.id,
        pr_number=8,
        expected_revision=retired_before.revision,
    )
    case.observer = NativeServiceObserver(
        "project",
        host=case.host,
        archived_workspaces=tuple(row["id"] for row in case.host.workspaces),
        clock=lambda: 100,
    )
    result = invoke(case, request=second)
    after = case.store.load(snapshot.id)
    assert len(after.bindings["ownership_transfers"]) == 2
    assert after.bindings["ownership_retirement"] == retired_before.bindings["ownership_retirement"]
    assert transfer.read_release(case.store, snapshot.id, case.unit.id) == first["record"]
    assert transfer.read_release(case.store, snapshot.id, second_spec.id) == result["record"]
    assert after.units == retired_before.units and after.outbox == retired_before.outbox


def test_predecessor_source_uses_frozen_preparation_selector_not_new_active_default(case):
    from bmad_loop.campaign_model import SourceSelector, source_to_dict

    snapshot = case.store.load(case.before.id)
    source = SourceSelector("tickets", id=case.unit.source)
    snapshot.spec = dataclasses.replace(snapshot.spec, sources=(source,))
    binding = {
        "effective_source": source_to_dict(dataclasses.replace(source, initiative="original"))
    }
    frozen = transfer._previous_source(snapshot, case.unit.id, binding)
    assert frozen.spec.sources[0].initiative == "original"
    assert snapshot.spec.sources[0].initiative == ""
    for field, value in (("id", "another-source"), ("kind", "stories")):
        wrong = copy.deepcopy(binding)
        wrong["effective_source"][field] = value
        with pytest.raises(CampaignError, match="source identity"):
            transfer._previous_source(snapshot, case.unit.id, wrong)
    snapshot.spec = dataclasses.replace(
        snapshot.spec, sources=(dataclasses.replace(source, initiative="different"),)
    )
    with pytest.raises(CampaignError, match="initiative conflicts"):
        transfer._previous_source(snapshot, case.unit.id, binding)
