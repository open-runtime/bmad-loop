"""Snapshot copies retain isolation, shared references and specialized journals."""

import copy

from test_campaign_database import state

from bmad_loop.campaign_database import EventJournal
from bmad_loop.campaign_store import CampaignStore, add_action


def test_snapshot_clone_isolates_nested_mutation_and_preserves_aliases():
    snapshot = state()
    shared = {"items": ["original"], "tuple": ("a", {"nested": [1]})}
    snapshot.bindings["shared"] = shared
    setattr(snapshot, "attached_metadata", shared)
    snapshot.units[next(iter(snapshot.units))].evidence["shared"] = shared
    action = add_action(snapshot, "metadata", "", shared)
    cloned = snapshot.clone()
    assert cloned == copy.deepcopy(snapshot)
    assert cloned.bindings["shared"] is cloned.units[next(iter(cloned.units))].evidence["shared"]
    assert getattr(cloned, "attached_metadata") is cloned.bindings["shared"]
    cloned.bindings["shared"]["items"].append("changed")
    cloned.bindings["shared"]["tuple"][1]["nested"].append(2)
    assert snapshot.bindings["shared"]["items"] == ["original"]
    assert snapshot.bindings["shared"]["tuple"][1]["nested"] == [1]
    assert cloned.outbox[0] is not action


def test_snapshot_clone_preserves_container_cycles():
    snapshot = state()
    cyclic = []
    pair = (cyclic,)
    cyclic.append(pair)
    snapshot.bindings["cycle"] = cyclic
    cloned = snapshot.clone()
    assert cloned.bindings["cycle"][0][0] is cloned.bindings["cycle"]
    assert cloned.bindings["cycle"] is not cyclic


def test_snapshot_clone_keeps_lazy_journal_independent(tmp_path):
    store = CampaignStore(tmp_path)
    snapshot = state()
    snapshot.events = [{"kind": "old", "nested": {"value": 1}}]
    snapshot = store.create(snapshot)
    snapshot.events[0]
    clone = snapshot.clone()
    clone.events[0]["nested"]["value"] = 2
    assert snapshot.events[0]["nested"]["value"] == 1
    assert isinstance(clone.events, EventJournal) and isinstance(snapshot.events, EventJournal)
    assert clone.events.database.path == snapshot.events.database.path
