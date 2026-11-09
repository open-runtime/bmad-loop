"""Real boundary/intake producers keep current obligations in bounded stage input."""

import copy
import dataclasses
import hashlib
import json
from pathlib import Path
from typing import cast

from test_campaign_intake_flow import snapshot as snapshot
from test_campaign_stage_prompt import indexed_record

from bmad_loop.campaign_stage_prompt import MAX_INLINE_CONTEXT_BYTES, prepare_context


def restored(projected):
    """Read every indexed value and reconstruct the exact original nested packet."""
    index = json.loads(Path(projected["retrievable_context"]["index"]["path"]).read_text())
    bulk = {}
    for key, layout in index["layout"].items():
        if layout["kind"] == "object":
            bulk[key] = {
                **layout["fields"],
                **{name: [None] * count for name, count in layout["collections"].items()},
            }
        elif layout["kind"] == "array":
            bulk[key] = [None] * layout["count"]
        elif layout["kind"] == "mapping":
            bulk[key] = {}
    for position, row in enumerate(index["records"]):
        if index["layout"][row["key"]]["kind"] == "value":
            bulk[row["key"]] = indexed_record(index, position)
            continue
        target = bulk[row["key"]]
        if row["collection"] and row["collection"] != "_fields":
            target = target[row["collection"]]
        target[row["position"]] = indexed_record(index, position)
    raw = json.dumps(bulk, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    assert hashlib.sha256(raw).hexdigest() == index["source_sha256"]
    result = copy.deepcopy(projected)
    result.pop("retrievable_context")
    for key, value in bulk.items():
        if key.startswith("/"):
            parts = key[1:].split("/")
            target = result
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = value
            target["retrievable_fields"].pop(parts[-1])
            if not target["retrievable_fields"]:
                target.pop("retrievable_fields")
        else:
            result[key] = value
    return result


def test_actual_48_unit_boundary_portfolio_is_indexed_without_dropping_owner_reviews(
    project, tmp_path
):
    from test_campaign_boundary_sync import Gateway, setup

    from bmad_loop import campaign_boundary_sync as boundary

    runtime, snapshot, unit = setup(project, tmp_path)
    gateway = cast(Gateway, runtime.gateway(snapshot))
    units = [unit]
    for position in range(1, 48):
        sibling = copy.deepcopy(unit)
        sibling.spec = dataclasses.replace(
            unit.spec, id=f"source:e{position // 2}:s{position}", kind="story"
        )
        sibling.bindings["issue"] = {"number": position + 1}
        sibling.bindings["pr"] = {"number": position + 2}
        units.append(sibling)
    snapshot.units = {row.id: row for row in units}
    issues = [
        {
            **gateway.item,
            "id": 1000 + position,
            "number": position + 1,
            "body": "Sibling acceptance context " * 60,
        }
        for position in range(48)
    ]
    pulls = [
        {
            **gateway.pr,
            "id": 2000 + position,
            "number": position + 2,
            "body": "Sibling implementation context " * 60,
        }
        for position in range(48)
    ]
    gateway.issues = lambda **kwargs: copy.deepcopy(issues)
    gateway.pulls = lambda: copy.deepcopy(pulls)
    gateway.notes = [
        {
            "id": 9001,
            "body": "Owner: preserve every accepted criterion",
            "updated_at": "2026-11-06T00:00:00Z",
            "user": {"login": "owner"},
        }
    ]
    gateway.review_rows = [{"id": 9002, "state": "CHANGES_REQUESTED", "body": "Fix startup"}]
    inputs = boundary.collect(runtime, snapshot, unit, action_id="48-unit-boundary")
    context = {
        "unit": dataclasses.asdict(unit.spec),
        "github_stage_inputs": inputs,
        "feedback": [{"required": True, "body": "Keep requested review fixes"}],
    }
    assert len(snapshot.units) == 48
    assert len(json.dumps(context).encode()) > MAX_INLINE_CONTEXT_BYTES
    projected, descriptors = prepare_context(tmp_path / "stage", context)
    assert len(json.dumps(projected).encode()) < MAX_INLINE_CONTEXT_BYTES
    assert projected["github_stage_inputs"]["github"] == inputs["github"]
    assert projected["unit"] == context["unit"]
    assert projected["feedback"] == context["feedback"]
    assert projected["github_stage_inputs"]["complete"] == inputs["complete"]
    assert descriptors and json.loads(json.dumps(restored(projected))) == json.loads(
        json.dumps(context)
    )


def test_actual_intake_and_prior_planning_packets_preserve_live_amendment_obligations(
    tmp_path, snapshot
):
    from test_campaign_intake_flow import ingest, observation

    from bmad_loop import campaign_intake_flow, campaign_planning_flow

    original = next(iter(snapshot.units.values()))
    for position in range(1, 48):
        sibling = copy.deepcopy(original)
        sibling.spec = dataclasses.replace(
            original.spec,
            id=f"source:e{position // 2}:s{position}",
            acceptance=("Preserve existing sibling acceptance " * 20,),
        )
        snapshot.units[sibling.id] = sibling
    version = ingest(snapshot, observation(body="Owner amendment: also prove restart durability"))
    packet = campaign_intake_flow.packet(snapshot, [version])
    original.evidence["planning_flow"] = {
        "stages": {"requirements": {"artifact": packet}},
        "findings": [{"body": "Required: retain original acceptance"}],
    }
    context = {
        "unit": dataclasses.asdict(original.spec),
        "intake": packet,
        "prior_artifacts": [packet],
        "acceptance_ledger": [{"id": "original", "text": "Keep behavior"}],
        "amendment_constraints": "Do not waive current criteria or replace the existing PR",
        **campaign_planning_flow.context(snapshot, original.id),
    }
    assert len(json.dumps(context).encode()) > MAX_INLINE_CONTEXT_BYTES
    projected, descriptors = prepare_context(tmp_path / "amendment", context)
    assert len(json.dumps(projected).encode()) < MAX_INLINE_CONTEXT_BYTES
    for key in ("observations", "scope_authority", "result_contract", "intake_coordinates"):
        assert projected["intake"][key] == packet[key]
    for key in ("unit", "acceptance_ledger", "amendment_constraints", "required_findings"):
        assert projected[key] == context[key]
    assert descriptors and json.loads(json.dumps(restored(projected))) == json.loads(
        json.dumps(context)
    )


def test_nested_intake_inventory_shards_pull_collection_above_single_artifact_bound(tmp_path):
    from test_campaign_stage_prompt import large_context

    from bmad_loop.campaign_stage_prompt import MAX_PACKET_BYTES

    original = large_context()
    inventory = original["inventory"]
    inventory["pulls"] *= 4
    assert len(json.dumps(inventory["pulls"]).encode()) > MAX_PACKET_BYTES
    context = {
        "unit": original["unit"],
        "intake": {
            "inventory": inventory,
            "observations": [{"body": "Current owner amendment remains exact"}],
        },
    }
    projected, descriptors = prepare_context(tmp_path / "nested-inventory", context)
    assert projected["intake"]["observations"] == context["intake"]["observations"]
    assert len(json.dumps(projected).encode()) < MAX_INLINE_CONTEXT_BYTES
    assert len(descriptors) > 10
    assert all(row["size_bytes"] <= MAX_PACKET_BYTES for row in descriptors)
    assert json.loads(json.dumps(restored(projected))) == json.loads(json.dumps(context))
