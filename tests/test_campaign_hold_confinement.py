"""Takeover records are read from directory handles, never redirected ancestors."""

import dataclasses
import json
import os
from types import SimpleNamespace

import pytest
from test_campaign_core import make_snapshot

from bmad_loop import campaign_hold, campaign_takeover, platform_util
from bmad_loop.campaign_model import CampaignError, Hold
from bmad_loop.campaign_store import CampaignStore


@pytest.fixture(autouse=True)
def anchored_reads():
    if not platform_util.HANDLE_ANCHORED_WRITES:
        pytest.skip("campaign hold channel requires anchored reads")


def link_or_skip(path, target):
    try:
        path.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip("host cannot create directory symlinks: " + str(exc))


@pytest.mark.parametrize("replacement_record", [False, True])
def test_redirected_hold_directory_is_held_even_if_target_looks_released(
    tmp_path, replacement_record
):
    path = campaign_hold.channel(tmp_path, "unit")
    path.parent.mkdir()
    assert not campaign_hold.active(path, campaign="campaign", unit="unit")
    campaign_hold.write(path, campaign="campaign", unit="unit", hold="operator")
    path.parent.rename(tmp_path / "retained-holds")
    target = tmp_path / "outside"
    target.mkdir()
    if replacement_record:
        campaign_hold.write(target / path.name, campaign="campaign", unit="unit", hold="")
    link_or_skip(path.parent, target)
    assert campaign_hold.active(path, campaign="campaign", unit="unit")
    path.parent.unlink()
    (tmp_path / "retained-holds").rename(path.parent)
    campaign_hold.write(path, campaign="campaign", unit="unit", hold="")
    assert not campaign_hold.active(path, campaign="campaign", unit="unit")


def test_missing_initialized_directory_is_held_but_missing_record_is_not(tmp_path):
    path = campaign_hold.channel(tmp_path, "unit")
    path.parent.mkdir()
    assert not campaign_hold.active(path, campaign="campaign", unit="unit")
    campaign_hold.write(path, campaign="campaign", unit="unit", hold="operator")
    path.parent.rename(tmp_path / "preserved")
    assert campaign_hold.active(path, campaign="campaign", unit="unit")
    (tmp_path / "preserved").rename(path.parent)
    campaign_hold.write(path, campaign="campaign", unit="unit", hold="")
    assert not campaign_hold.active(path, campaign="campaign", unit="unit")


def test_swap_after_parent_open_reads_retained_hold_handle(tmp_path, monkeypatch):
    path = campaign_hold.channel(tmp_path, "unit")
    path.parent.mkdir()
    campaign_hold.write(path, campaign="campaign", unit="unit", hold="operator")
    target = tmp_path / "outside"
    target.mkdir()
    campaign_hold.write(target / path.name, campaign="campaign", unit="unit", hold="")
    original = platform_util.open_at
    swapped = False

    def swap(parent, name, flags, mode=0o600):
        nonlocal swapped
        handle = original(parent, name, flags, mode)
        if name == "holds" and not swapped:
            swapped = True
            path.parent.rename(tmp_path / "retained-holds")
            try:
                link_or_skip(path.parent, target)
            except BaseException:
                os.close(handle)
                raise
        return handle

    monkeypatch.setattr(platform_util, "open_at", swap)
    assert campaign_hold.active(path, campaign="campaign", unit="unit")
    assert swapped


def test_redirected_ancestor_above_campaign_does_not_pin_foreign_empty_root(tmp_path):
    parent = tmp_path / "authority-parent"
    path = campaign_hold.channel(parent / "campaign", "unit")
    path.parent.mkdir(parents=True)
    campaign_hold.write(path, campaign="campaign", unit="unit", hold="operator")
    parent.rename(tmp_path / "retained-authority")
    target = tmp_path / "outside"
    (target / "campaign").mkdir(parents=True)
    link_or_skip(parent, target)
    assert campaign_hold.active(path, campaign="campaign", unit="unit")


@pytest.mark.parametrize("storage", ["json", "sqlite"])
def test_store_mints_channel_directory_and_never_recreates_lost_authority(tmp_path, storage):
    store = CampaignStore(tmp_path / "store", storage=storage)
    saved = store.create(make_snapshot())
    selected = next(iter(saved.units.values()))
    path = campaign_hold.channel(store.directory(saved.id), selected.id)
    assert path.parent.is_dir()
    assert not campaign_hold.active(path, campaign=saved.id, unit=selected.id)
    campaign_hold.write(path, campaign=saved.id, unit=selected.id, hold="operator")
    path.parent.rename(path.parent.with_name("preserved-holds"))
    store.save(saved, saved.revision)
    assert not path.parent.exists()
    assert campaign_hold.active(path, campaign=saved.id, unit=selected.id)


def test_legacy_store_migration_materializes_committed_hold_without_overwriting_records(tmp_path):
    store = CampaignStore(tmp_path / "store", storage="json")
    saved = store.create(make_snapshot())
    directory = store.directory(saved.id)
    state = directory / "snapshot.json"
    raw = json.loads(state.read_text(encoding="utf-8"))
    raw["bindings"].pop("hold_channels_version")
    key = next(iter(raw["units"]))
    raw["units"][key]["hold"] = dataclasses.asdict(Hold("operator", "Owner takeover", id="hold-1"))
    state.write_text(json.dumps(raw), encoding="utf-8")
    other = next(value for value in raw["units"] if value != key)
    retained = campaign_hold.channel(directory, other)
    campaign_hold.write(
        retained, campaign=saved.id, unit=other, hold="retained-unacknowledged-hold"
    )
    before = retained.read_bytes()
    legacy = store.load(saved.id)
    upgraded = store.save(legacy, legacy.revision)
    assert upgraded.bindings["hold_channels_version"] == 1
    assert retained.read_bytes() == before
    assert campaign_hold.active(campaign_hold.channel(directory, key), campaign=saved.id, unit=key)


def test_legacy_conflicting_record_has_typed_refusal_and_is_not_overwritten(tmp_path):
    snapshot = make_snapshot()
    selected = next(iter(snapshot.units.values()))
    selected.hold = Hold("operator", "Owner takeover", id="held")
    directory = tmp_path / "campaign"
    path = campaign_hold.channel(directory, selected.id)
    path.parent.mkdir(parents=True)
    campaign_hold.write(path, campaign=snapshot.id, unit=selected.id, hold="")
    before = path.read_bytes()
    with pytest.raises(CampaignError, match="contradicts its committed operator hold"):
        campaign_hold.initialize(directory, snapshot)
    assert path.read_bytes() == before
    assert "hold_channels_version" not in snapshot.bindings


def test_release_cannot_recreate_lost_directory_and_clear_sibling_hold(tmp_path):
    snapshot = make_snapshot()
    first, second = list(snapshot.units.values())[:2]
    first.hold = Hold("operator", "Owner takeover", id="first")
    second.hold = Hold("operator", "Owner takeover", id="second")
    store = CampaignStore(tmp_path / "store")
    saved = store.create(snapshot)
    first, second = saved.units[first.id], saved.units[second.id]
    sibling = campaign_hold.channel(store.directory(saved.id), second.id)
    sibling.parent.rename(sibling.parent.with_name("preserved-holds"))
    with pytest.raises(CampaignError, match="hold authority is missing"):
        campaign_takeover.apply(
            SimpleNamespace(store=store),
            saved,
            SimpleNamespace(unit=first.id),
            {"hold_id": "first", "claim": "", "head": ""},
        )
    assert not sibling.parent.exists()
    assert campaign_hold.active(sibling, campaign=saved.id, unit=second.id)
    committed = store.load(saved.id)
    assert committed.units[first.id].hold and committed.units[second.id].hold


def test_queued_legacy_release_reads_migrated_persisted_hold_before_control(project, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from bmad_loop.campaign_runtime import CampaignRuntime
    from bmad_loop.campaign_supervisor import CampaignSupervisor

    snapshot = make_snapshot()
    selected = next(iter(snapshot.units.values()))
    selected.hold = Hold("operator", "Owner takeover", id="legacy-hold")
    store = CampaignStore(tmp_path / "store", storage="json")
    saved = store.create(snapshot)
    directory = store.directory(saved.id)
    path = directory / "snapshot.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["bindings"].pop("hold_channels_version")
    path.write_text(json.dumps(raw), encoding="utf-8")
    held_path = campaign_hold.channel(directory, selected.id)
    held_path.unlink()
    held_path.parent.rmdir()
    store.enqueue(saved.id, "release", unit=selected.id)
    runtime = CampaignRuntime(project.project, store)
    runtime.prepare_resume = lambda _: False
    original = runtime.control
    called = []

    def control(current, command):
        assert current.bindings["hold_channels_version"] == 1
        assert store.load(current.id).bindings["hold_channels_version"] == 1
        assert campaign_hold.active(held_path, campaign=current.id, unit=selected.id)
        called.append(command["kind"])
        return original(current, command)

    runtime.control = control
    supervisor = CampaignSupervisor(store, runtime)
    with ThreadPoolExecutor(max_workers=1) as executor:
        migrated = supervisor._reconciled(saved.id, executor)
    assert called == ["release"]
    action = SimpleNamespace(
        unit=selected.id, payload=migrated.units[selected.id].bindings["operator_release"]
    )
    proof = campaign_takeover.observe(runtime, migrated, action)
    campaign_takeover.apply(runtime, migrated, action, proof)
    saved = store.save(migrated, migrated.revision)
    assert saved.units[selected.id].hold is None
    assert not campaign_hold.active(held_path, campaign=saved.id, unit=selected.id)
