"""Closed final selection preserves conditional source rules without inventing controls."""

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_campaign_handoff_context import budget, capture, native_row, quotes

from bmad_loop import campaign_summary_final as final
from bmad_loop.campaign_fidelity import semantic_contract
from bmad_loop.campaign_handoff_obligations import AUDIT_SCHEMA
from bmad_loop.campaign_model import CampaignError
from bmad_loop.campaign_summary import (
    FINAL_PROMPT_VERSION,
    LEGACY_FINAL_PROMPT_VERSION,
    SummaryResultRejected,
    corrective_spec,
    final_input_size,
    load_input,
    plan_maps,
    plan_reduction,
    project_final_input,
    project_reduction_input,
    split_reduction_input,
    summary_prompt,
    summary_schema,
    validate_summary,
)
from bmad_loop.campaign_transcripts import digest, encoded, load_events


@pytest.fixture
def sources(tmp_path):
    rule = "An explicit operator hold remains authoritative until authenticated release."
    history = capture(tmp_path, [native_row("codex", rule)])
    events = load_events(history)
    leaf = plan_maps(events, budget(), tmp_path / "maps")[0]
    raw = {
        "schema_version": 1,
        "job_id": leaf.id,
        "input_sha256": leaf.input_sha256,
        "covered_ids": list(leaf.primary_ids),
        "gaps": [],
        "facts": [
            {
                "kind": "owner_request",
                "text": "Preserve explicit holds",
                "citations": [events[0].id],
                "source_quote": rule,
            },
            {
                "kind": "nuance",
                "text": "A standing operator hold exists.",
                "citations": [events[0].id],
            },
            {"kind": "progress", "text": "Review remains unfinished.", "citations": [events[0].id]},
        ],
    }
    child = validate_summary(leaf, raw, events)
    reduce = plan_reduction(
        ((leaf, child),), budget(), tmp_path / "reduce", owner_quotes=quotes(history, child)
    )[0]
    return events, child, reduce


def selection(job, ids=None):
    return {
        "schema_version": 2,
        "job_id": job.id,
        "input_sha256": job.input_sha256,
        "covered_ids": list(job.primary_ids),
        "selected_fact_ids": list(load_input(job)["fact_catalog"]) if ids is None else ids,
    }


def test_conditional_rule_and_child_hold_claim_are_never_new_control_text(tmp_path, sources):
    events, child, reduce = sources
    projected = project_reduction_input(reduce, tmp_path / "projected")
    assert "id" not in load_input(projected)["children"][0]["summary"]["facts"][0]
    job = project_final_input(projected, tmp_path / "final")
    payload = load_input(job)
    assert job.input_tokens == final_input_size(projected) == len(encoded(payload))
    assert payload["children"][0]["summary"] == child
    assert project_final_input(job, tmp_path / "same") == job
    raw = selection(job, list(reversed(payload["fact_catalog"])))
    validated = validate_summary(job, raw, events)
    assert set(summary_schema(job)["properties"]) == set(raw)
    assert validated["final_contract"] == FINAL_PROMPT_VERSION == final.VERSION
    assert validated["omitted_fact_ids"] == []
    assert payload["owner_quotes"] == load_input(reduce)["owner_quotes"]
    assert [fact["selected_from"]["catalog_id"] for fact in validated["facts"]] == raw[
        "selected_fact_ids"
    ]
    originals = {fact["id"]: fact for fact in child["facts"]}
    for actual in validated["facts"]:
        ref = actual["selected_from"]
        original = originals[ref["fact_id"]]
        assert ref["child_result_sha256"] == digest(child)
        assert ref["fact_id"] == original["id"]
        assert actual["text"] == final.attribution_text(child["job_id"], original["text"])
        assert json.loads(actual["text"].split(": ", 1)[1]) == original["text"]
        assert actual["citations"] == original["citations"]
        assert actual["authority"] == "context"
        final.validate_attribution(actual)
    assert "current_state" not in validated and "holds" not in validated
    assert "facts" not in raw and "gaps" not in raw


@pytest.mark.parametrize(
    "field,value",
    [("text", "Do not merge"), ("current_hold", True), ("facts", []), ("gaps", []), ("groups", [])],
)
def test_model_cannot_write_prose_controls_or_clear_gaps(tmp_path, sources, field, value):
    events, _, reduce = sources
    job = project_final_input(reduce, tmp_path / "final")
    raw = {**selection(job), field: value}
    with pytest.raises(SummaryResultRejected, match="model-written content"):
        validate_summary(job, raw, events)


@pytest.mark.parametrize(
    "change", ["duplicate", "unknown", "wrong-child", "wrong-input", "schema-bool"]
)
def test_closed_selection_rejects_stale_or_invented_references(tmp_path, sources, change):
    events, _, reduce = sources
    job = project_final_input(reduce, tmp_path / "final")
    raw = selection(job)
    if change == "duplicate":
        raw["selected_fact_ids"].append(raw["selected_fact_ids"][0])
    elif change == "unknown":
        raw["selected_fact_ids"] = ["unknown"]
    elif change == "wrong-child":
        raw["covered_ids"] = ["another-child"]
    elif change == "wrong-input":
        raw["input_sha256"] = "0" * 64
    else:
        raw["schema_version"] = True
    with pytest.raises(SummaryResultRejected):
        validate_summary(job, raw, events)


def test_omitted_source_claims_and_gaps_remain_complete(tmp_path, sources):
    events, child, reduce = sources
    source = load_input(reduce)
    source["children"][0]["summary"]["gaps"] = ["Missing a native tail"]
    path = tmp_path / "gapped.json"
    path.write_bytes(encoded(source))
    reduce = replace(reduce, input_path=str(path), input_sha256=digest(source))
    job = project_final_input(reduce, tmp_path / "final")
    raw = selection(job, [])
    validated = validate_summary(job, raw, events)
    assert validated["gaps"] == ["Missing a native tail"]
    assert validated["complete"] is False
    assert validated["facts"] == []
    assert set(validated["omitted_fact_ids"]) == set(load_input(job)["fact_catalog"])
    assert len(validated["omitted_fact_ids"]) == len(child["facts"])
    assert load_input(job)["owner_quotes"]  # Selection cannot omit mandatory source text.


def test_changed_full_source_bytes_refuse_even_if_json_is_equivalent(tmp_path, sources):
    events, _, reduce = sources
    job = project_final_input(
        project_reduction_input(reduce, tmp_path / "projected"), tmp_path / "final"
    )
    raw = selection(job)
    original = Path(reduce.input_path)
    original.write_bytes(original.read_bytes() + b"\n")
    with pytest.raises(CampaignError, match="summary input changed since planning"):
        validate_summary(job, raw, events)


def test_catalog_identity_uses_full_child_result_not_projected_text(tmp_path, sources):
    _, _, reduce = sources
    payload = load_input(reduce)
    before = final.catalog(payload)
    altered = copy.deepcopy(payload)
    altered["children"][0]["summary"]["output_measurement"]["tokens"] += 1
    assert set(final.catalog(altered)).isdisjoint(before)
    altered = copy.deepcopy(payload)
    altered["children"][0]["summary"]["facts"].append(
        copy.deepcopy(altered["children"][0]["summary"]["facts"][0])
    )
    with pytest.raises(CampaignError, match="immutable source identity"):
        final.catalog(altered)


def test_expanded_attributed_claims_are_charged_to_artifact_budget(tmp_path, sources):
    events, _, reduce = sources
    job = project_final_input(reduce, tmp_path / "final")
    raw = selection(job)
    # The model's short selection fits; the actual successor-facing expansion does not.
    job = replace(job, artifact_bytes=len(encoded(raw)) + 128)
    assert len(encoded(raw)) < job.artifact_bytes
    with pytest.raises(SummaryResultRejected, match="Expanded final source claims exceed"):
        validate_summary(job, raw, events, observed_output_tokens=100)


def test_corrective_selection_prompt_exposes_findings_and_expanded_budget(tmp_path, sources):
    _, _, reduce = sources
    original = project_final_input(replace(reduce, artifact_bytes=9000), tmp_path / "final")
    correction = corrective_spec(
        original,
        attempt=1,
        receipt_sha256="rejected-receipt",
        reason="Select the omitted unfinished review and preserve its source attribution",
    )
    prompt = summary_prompt(correction)
    assert correction.correction_reason in prompt
    assert "findings as fallible context" in prompt
    assert (
        "Python-expanded artifact, including exact quoted text and provenance, must fit 9000 UTF-8 bytes"
        in prompt
    )
    assert summary_schema(correction)["properties"]["job_id"]["enum"] == [correction.id]
    assert set(summary_schema(correction)["properties"]) == set(selection(correction))
    assert load_input(correction) == load_input(original)


@pytest.mark.parametrize(
    "field,value",
    [
        ("text", "A standing hold exists"),
        ("authority", "owner"),
        ("id", "changed"),
        ("selected_from", {}),
    ],
)
def test_attribution_cannot_be_removed_from_rendered_claim(tmp_path, sources, field, value):
    events, _, reduce = sources
    job = project_final_input(reduce, tmp_path / "final")
    fact = validate_summary(job, selection(job), events)["facts"][0]
    fact[field] = value
    with pytest.raises(CampaignError, match="Final"):
        final.validate_attribution(fact)


def test_repartition_rebuilds_catalog_for_each_complete_child(tmp_path, sources):
    events, child, reduce = sources
    source = load_input(reduce)
    second = copy.deepcopy(child)
    second["job_id"] = "another-authenticated-child"
    source["children"].append({"job_id": second["job_id"], "summary": second})
    path = tmp_path / "two-children.json"
    path.write_bytes(encoded(source))
    reduce = replace(
        reduce,
        input_path=str(path),
        input_sha256=digest(source),
        primary_ids=(child["job_id"], second["job_id"]),
        dependencies=(child["job_id"], second["job_id"]),
    )
    job = project_final_input(reduce, tmp_path / "final")
    split = split_reduction_input(job, tmp_path / "split")
    assert len(split) == 2
    selected = set()
    for part in split:
        payload = load_input(part)
        assert len(payload["children"]) == 1
        assert part.input_tokens == final_input_size(part)
        assert set(payload["fact_catalog"]).isdisjoint(selected)
        selected.update(payload["fact_catalog"])
        assert validate_summary(part, selection(part), events)["complete"]
    assert selected == set(load_input(job)["fact_catalog"])


def test_v3_saved_artifacts_stay_readable_but_schema_is_not_v4(tmp_path, sources):
    events, child, reduce = sources
    legacy = replace(reduce, prompt_version=LEGACY_FINAL_PROMPT_VERSION)
    raw = {
        "schema_version": 1,
        "job_id": legacy.id,
        "input_sha256": legacy.input_sha256,
        "covered_ids": list(legacy.primary_ids),
        "facts": copy.deepcopy(child["facts"]),
        "gaps": [],
    }
    assert validate_summary(legacy, raw, events)["complete"]
    assert "facts" in summary_schema(legacy)["properties"]
    assert "Produce the final successor synthesis" in summary_prompt(legacy)
    modern = project_final_input(legacy, tmp_path / "v4")
    with pytest.raises(SummaryResultRejected):
        validate_summary(
            modern, {**raw, "job_id": modern.id, "input_sha256": modern.input_sha256}, events
        )


@pytest.mark.parametrize(
    "purpose,contract,expected",
    [
        ("handoff_map", "", "ba0203f7f4de8c2a9e4a0c074ca76202b38f62bc8cf9dd2a6f7b072ad10bdf04"),
        ("handoff_reduce", "", "73b0da0b841d6c632e078dc6294027605f7e5b8d4466442fcd193b2e0861e208"),
        ("handoff_audit", "", "492e578ff06295c8768ef330adf4851862a73906d185b836821c49d15a59d6c4"),
        (
            "constraint_reduce",
            "constraints-v1",
            "2ac6e9ed2459fcead2a26c2d33d9ac32f0f112cb8057f716deeab03ea2a8822c",
        ),
        (
            "constraint_audit",
            "constraints-v1",
            "9c36df8032bfc0a018639192e1c5e86126927d4da7d15f6685d513dbfa0f7d22",
        ),
        (
            "recovery_extract",
            "recovery-v1",
            "6fcb957459ce65d765ce2e2604b3cd2fb14234e3aa1ade231426861066b42265",
        ),
        (
            "recovery_audit",
            "recovery-v1",
            "ccebd3cf56a55a2264f5f37f412e5d6464f187171b59c3eed9e7d730582faa1c",
        ),
    ],
)
def test_unrelated_purpose_contracts_remain_exact(purpose, contract, expected):
    assert digest(semantic_contract(purpose, AUDIT_SCHEMA, contract)) == expected


def test_final_contracts_change_without_reusing_legacy_proof_headers():
    assert (
        digest(semantic_contract("handoff_final", AUDIT_SCHEMA))
        != "e7bb4f86ab6de032d34cbd93c4d78c212c56873d92aaaaf25727ef764961ee7d"
    )
    assert (
        digest(semantic_contract("handoff_final_audit", AUDIT_SCHEMA))
        != "006141d9e942da9ef5c7eacf2980f5668c827b4689655fe294ea9b90fbefc6b4"
    )
