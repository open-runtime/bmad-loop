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
    final_artifact_envelope_size,
    final_input_size,
    load_input,
    plan_maps,
    plan_reduction,
    project_final_input,
    project_reduction_input,
    split_reduction_input,
    summary_contract_current,
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
        "schema_version": 4,
        "job_id": job.id,
        "input_sha256": job.input_sha256,
        "covered_ids": list(job.primary_ids),
        "ranked_fact_ids": list(load_input(job)["fact_catalog"]) if ids is None else ids,
        "required_fact_ids": [],
        "excluded_fact_ids": [],
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
        "ranked_fact_ids"
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
    "change", ["duplicate", "unknown", "missing", "wrong-child", "wrong-input", "schema-bool"]
)
def test_closed_selection_rejects_stale_or_invented_references(tmp_path, sources, change):
    events, _, reduce = sources
    job = project_final_input(reduce, tmp_path / "final")
    raw = selection(job)
    if change == "duplicate":
        raw["ranked_fact_ids"].append(raw["ranked_fact_ids"][0])
    elif change == "unknown":
        raw["ranked_fact_ids"] = ["unknown"]
    elif change == "missing":
        raw["ranked_fact_ids"].pop()
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
    job = replace(job, artifact_bytes=9000)
    job = replace(job, artifact_bytes=final_artifact_envelope_size(job))
    raw = selection(job)
    validated = validate_summary(job, raw, events, observed_output_tokens=100)
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
    with pytest.raises(SummaryResultRejected, match="Final ranking envelope exceeds"):
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
    assert "Do not calculate byte sizes" in prompt
    assert "complete ranked_fact_ids permutation" in prompt
    assert "required_fact_ids" in prompt and "excluded_fact_ids" in prompt
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


def test_v4_saved_selection_validates_explicitly_without_current_grant(tmp_path, sources):
    events, _, reduce = sources
    source = load_input(reduce)
    payload = final.input_payload(
        source,
        {"path": reduce.input_path, "sha256": reduce.input_sha256, "job_id": reduce.id},
        version="history-final-v4",
    )
    path = tmp_path / "historical-v4.json"
    path.write_bytes(encoded(payload))
    old = replace(
        reduce,
        input_path=str(path),
        input_sha256=digest(payload),
        input_tokens=len(encoded(payload)),
        prompt_version="history-final-v4",
    )
    ids = list(payload["fact_catalog"])
    raw = {
        "schema_version": 2,
        "job_id": old.id,
        "input_sha256": old.input_sha256,
        "covered_ids": list(old.primary_ids),
        "selected_fact_ids": ids[:1],
    }
    value = validate_summary(old, raw, events)
    assert value["final_contract"] == "history-final-v4"
    assert value["selected_fact_ids"] == ids[:1]
    assert value["omitted_fact_ids"] == ids[1:]
    assert "packing" not in value and "omitted_facts" not in value
    final.validate_attribution(value["facts"][0], version="history-final-v4")
    assert summary_schema(old)["properties"]["schema_version"]["enum"] == [2]
    assert "selected_fact_ids" in summary_schema(old)["properties"]
    assert not summary_contract_current(
        {
            "purpose": "handoff_reduce",
            "coordinates": {
                "summary_stage": "final",
                "summary_spec": {"prompt_version": old.prompt_version},
            },
        }
    )
    current = project_final_input(old, tmp_path / "current")
    assert current.prompt_version == final.VERSION == "history-final-v6"
    assert current.input_sha256 != old.input_sha256
    assert "ranked_fact_ids" in summary_schema(current)["properties"]


def test_packing_omits_context_without_resolving_unfinished_work(tmp_path, sources):
    events, child, reduce = sources
    source = load_input(reduce)
    source["children"][0]["summary"]["facts"][-1]["kind"] = "next_action"
    path = tmp_path / "unfinished.json"
    path.write_bytes(encoded(source))
    job = project_final_input(
        replace(reduce, input_path=str(path), input_sha256=digest(source), artifact_bytes=9000),
        tmp_path / "packed",
    )
    job = replace(job, artifact_bytes=final_artifact_envelope_size(job))
    value = validate_summary(job, selection(job), events, observed_output_tokens=100)
    assert value["facts"] == []
    assert value["semantic_fidelity"] == "unqualified"
    assert value["ranked_fact_ids"] == value["omitted_fact_ids"]
    assert len(value["omitted_facts"]) == len(child["facts"])
    catalog = load_input(job)["fact_catalog"]
    for omitted in value["omitted_facts"]:
        assert omitted == {"catalog_id": omitted["catalog_id"], "reason": "artifact-byte-budget"}
        ref = catalog[omitted["catalog_id"]]
        original = next(
            f for f in source["children"][0]["summary"]["facts"] if f["id"] == ref["fact_id"]
        )
        assert ref["child_result_sha256"] == digest(source["children"][0]["summary"])
        if original["kind"] == "next_action":
            assert original["text"] == "Review remains unfinished."
    assert load_input(job)["owner_quotes"] == load_input(reduce)["owner_quotes"]
    assert len(encoded(value)) <= job.artifact_bytes


def test_impossible_envelope_refuses_before_request_schema_or_prompt(tmp_path, sources):
    _, _, reduce = sources
    job = project_final_input(replace(reduce, artifact_bytes=9000), tmp_path / "final")
    impossible = replace(job, artifact_bytes=1)
    for operation in (summary_schema, summary_prompt):
        with pytest.raises(CampaignError, match="before launch"):
            operation(impossible)
    with pytest.raises(CampaignError, match="before launch"):
        project_final_input(replace(reduce, artifact_bytes=1), tmp_path / "impossible")


def test_prior_final_proof_digests_are_stale_and_required_context_audit_is_versioned():
    current = semantic_contract("handoff_final", AUDIT_SCHEMA)
    prior = {
        **current,
        "prompt_version": "history-final-v4",
        "summary_schema_digest": digest(final.schema_template(version="history-final-v4")),
        "summary_job_schema_digest": digest(final.schema_template(version="history-final-v4")),
    }
    assert current["prompt_version"] == "history-final-v6"
    assert digest(current) != digest(prior)
    audit = semantic_contract("handoff_final_audit", AUDIT_SCHEMA)
    assert audit["prompt_version"] == "history-final-audit-v3"
    assert audit["summary_schema_digest"] != prior["summary_schema_digest"]


def ranked_payload(texts):
    child = {
        "job_id": "child",
        "structural_coverage": True,
        "gaps": [],
        "facts": [
            {
                "id": "fact-" + str(index),
                "kind": "next_action",
                "text": text,
                "citations": ["event"],
                "authority": "context",
            }
            for index, text in enumerate(texts)
        ],
    }
    payload = final.input_payload(
        {"children": [{"job_id": "child", "summary": child}], "owner_quotes": {}},
        {"path": "source", "sha256": "a" * 64, "job_id": "source"},
    )
    ids = list(payload["fact_catalog"])
    result = {
        "schema_version": 4,
        "job_id": "job",
        "input_sha256": "b" * 64,
        "covered_ids": ["child"],
        "ranked_fact_ids": ids,
        "required_fact_ids": [],
        "excluded_fact_ids": [],
    }
    return result, payload


def packing_metadata(limit, result):
    return {
        "structural_coverage": True,
        "semantic_fidelity": "unqualified",
        "complete": True,
        "output_measurement": {"kind": "native-receipt", "tokens": 32768},
        "reduction_budget": {"artifact_byte_limit": limit, "result_bytes": len(encoded(result))},
    }


def pack(result, payload, limit, metadata):
    return final.expand(
        result,
        payload,
        job_id="job",
        input_sha256="b" * 64,
        children=("child",),
        artifact_bytes=limit,
        metadata=metadata,
    )


def test_exact_full_normalized_unicode_boundary_and_one_byte_below():
    raw, payload = ranked_payload(
        ['First: 🧭 café\\"\n', "Second unfinished review: 日本語", "Third: 😀"]
    )
    raw["ranked_fact_ids"].reverse()
    limit = 10000
    for _ in range(4):
        full = pack(raw, payload, 0, packing_metadata(limit, raw))
        measured = len(encoded(full))
        if measured == limit:
            break
        limit = measured
    else:
        raise AssertionError("Byte-length fixed point did not converge")
    exact = pack(raw, payload, limit, packing_metadata(limit, raw))
    assert len(encoded(exact)) == limit
    assert exact["selected_fact_ids"] == raw["ranked_fact_ids"]
    assert exact["omitted_facts"] == []
    assert exact["output_measurement"]["tokens"] == 32768
    assert exact["reduction_budget"]["result_bytes"] == len(encoded(raw))
    smaller = pack(raw, payload, limit - 1, packing_metadata(limit - 1, raw))
    assert smaller["selected_fact_ids"] == raw["ranked_fact_ids"][:-1]
    assert smaller["omitted_fact_ids"] == raw["ranked_fact_ids"][-1:]
    assert len(encoded(smaller)) <= limit - 1
    for fact in exact["facts"]:
        final.validate_attribution(fact)
    assert len(encoded(exact)) > len(encoded(exact).decode())  # Actual UTF-8, not character count.


def test_packing_skips_oversized_whole_fact_then_preserves_remaining_rank_order():
    raw, payload = ranked_payload(
        ["Large context " * 1000, "Second unfinished task", "Third unfinished task"]
    )
    limit = 3000
    value = pack(raw, payload, limit, packing_metadata(limit, raw))
    assert value["selected_fact_ids"] == raw["ranked_fact_ids"][1:]
    assert value["omitted_fact_ids"] == raw["ranked_fact_ids"][:1]
    assert [f["selected_from"]["catalog_id"] for f in value["facts"]] == raw["ranked_fact_ids"][1:]
    assert value["omitted_facts"] == [
        {"catalog_id": raw["ranked_fact_ids"][0], "reason": "artifact-byte-budget"}
    ]
    original = payload["children"][0]["summary"]["facts"]
    assert value["facts"][0]["text"] == final.attribution_text("child", original[1]["text"])
    assert original[0]["text"] == "Large context " * 1000
    assert len(encoded(value)) <= limit


def test_packing_serializes_each_fact_only_once(monkeypatch):
    raw, payload = ranked_payload([f"Task {i} 🧭" for i in range(100)])
    calls = []
    original = final.encoded

    def count(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(final, "encoded", count)
    value = pack(raw, payload, 50000, packing_metadata(50000, raw))
    assert len(calls) == 2 * len(raw["ranked_fact_ids"]) + 2
    assert len(encoded(value)) <= 50000
    assert len(value["selected_fact_ids"]) + len(value["omitted_fact_ids"]) == 100


def test_no_source_facts_has_valid_empty_complete_ranking():
    raw, payload = ranked_payload([])
    value = pack(raw, payload, 2000, packing_metadata(2000, raw))
    assert value["ranked_fact_ids"] == value["selected_fact_ids"] == value["omitted_fact_ids"] == []
    assert value["facts"] == value["omitted_facts"] == []
    assert len(encoded(value)) < 2000


def test_required_reviews_survive_optional_pressure_and_excluded_hold_stays_source_only():
    # Authored analogue of the actual 153 failure: four separately supported
    # pending reviews and a misleading mixed action, not a replayed native result.
    raw, payload = ranked_payload(
        [
            "Background context " * 300,
            "Do not merge; finish claim 3 review.",
            *[f"Claim {i} review remains pending." for i in range(4)],
            "Other context " * 300,
        ]
    )
    ids = raw["ranked_fact_ids"]
    raw["required_fact_ids"] = list(reversed(ids[2:6]))
    raw["excluded_fact_ids"] = ids[1:2]
    before = copy.deepcopy(payload)
    value = pack(raw, payload, 6000, packing_metadata(6000, raw))
    assert value["selected_fact_ids"] == ids[2:6]
    assert [json.loads(f["text"].split(": ", 1)[1]) for f in value["facts"]] == [
        f"Claim {i} review remains pending." for i in range(4)
    ]
    assert value["omitted_facts"] == [
        {"catalog_id": ids[0], "reason": "artifact-byte-budget"},
        {"catalog_id": ids[1], "reason": "model-excluded-context"},
        {"catalog_id": ids[6], "reason": "artifact-byte-budget"},
    ]
    assert payload == before
    assert payload["children"][0]["summary"]["facts"][1]["text"].startswith("Do not merge")
    assert value["semantic_fidelity"] == "unqualified"
    assert all(f["authority"] == "context" for f in value["facts"])
    assert len(encoded(value)) <= 6000
    final.validate_packing(value)


def test_required_facts_fit_exactly_or_refuse_instead_of_silently_skipping():
    raw, payload = ranked_payload(["Review 日本語 🧭", "Second unfinished review"])
    raw["required_fact_ids"] = list(raw["ranked_fact_ids"])
    limit = 10000
    for _ in range(4):
        measured = len(encoded(pack(raw, payload, 0, packing_metadata(limit, raw))))
        if measured == limit:
            break
        limit = measured
    else:
        raise AssertionError("Exact normalized boundary did not stabilize")
    exact = pack(raw, payload, limit, packing_metadata(limit, raw))
    assert len(encoded(exact)) == limit
    assert exact["selected_fact_ids"] == raw["ranked_fact_ids"]
    with pytest.raises(CampaignError, match="required source facts exceed"):
        pack(raw, payload, limit - 1, packing_metadata(limit - 1, raw))


@pytest.mark.parametrize("field", ["required_fact_ids", "excluded_fact_ids"])
@pytest.mark.parametrize("damage", ["unknown", "duplicate", "wrong-type", "missing"])
def test_classified_references_must_be_unique_known_lists(field, damage):
    raw, payload = ranked_payload(["Review pending"])
    key = raw["ranked_fact_ids"][0]
    if damage == "unknown":
        raw[field] = ["f" * 64]
    elif damage == "duplicate":
        raw[field] = [key, key]
    elif damage == "wrong-type":
        raw[field] = key
    else:
        del raw[field]
    with pytest.raises(CampaignError, match="unique ranked references|model-written content"):
        pack(raw, payload, 9000, packing_metadata(9000, raw))


def test_required_and_excluded_references_cannot_overlap():
    raw, payload = ranked_payload(["Review pending"])
    raw["required_fact_ids"] = raw["ranked_fact_ids"][:]
    raw["excluded_fact_ids"] = raw["ranked_fact_ids"][:]
    with pytest.raises(CampaignError, match="disjoint"):
        pack(raw, payload, 9000, packing_metadata(9000, raw))


def test_excluded_context_never_displays_even_with_unlimited_artifact_space():
    raw, payload = ranked_payload(["Unsupported standing hold", "Actual pending review"])
    raw["excluded_fact_ids"] = raw["ranked_fact_ids"][:1]
    value = pack(raw, payload, 0, packing_metadata(9000, raw))
    assert value["selected_fact_ids"] == raw["ranked_fact_ids"][1:]
    assert value["omitted_facts"] == [
        {"catalog_id": raw["ranked_fact_ids"][0], "reason": "model-excluded-context"}
    ]
    assert len(payload["fact_catalog"]) == 2


def test_v6_all_excluded_preflight_bounds_every_disjoint_classification_envelope():
    import itertools

    raw, payload = ranked_payload(["α", "🧭", "第三"])
    ids = raw["ranked_fact_ids"]
    raw["excluded_fact_ids"] = ids[:]

    def envelope(value):
        return final.minimum_artifact_bytes(
            value,
            payload,
            job_id="job",
            input_sha256="b" * 64,
            children=("child",),
            metadata=packing_metadata(9999, value),
        )

    worst = envelope(raw)
    for labels in itertools.product(range(3), repeat=len(ids)):
        candidate = {
            **raw,
            "required_fact_ids": [identity for identity, label in zip(ids, labels) if label == 1],
            "excluded_fact_ids": [identity for identity, label in zip(ids, labels) if label == 2],
        }
        assert envelope(candidate) <= worst


@pytest.mark.parametrize(
    "damage",
    ["required-lost", "excluded-selected", "order", "reason", "omitted-id", "fact-id", "owner"],
)
def test_retained_v6_packing_rejects_inconsistent_presentation(damage):
    raw, payload = ranked_payload(["Optional context", "Required review", "Excluded claim"])
    ids = raw["ranked_fact_ids"]
    raw["required_fact_ids"] = ids[1:2]
    raw["excluded_fact_ids"] = ids[2:]
    value = pack(raw, payload, 9000, packing_metadata(9000, raw))
    assert value["selected_fact_ids"] == [ids[1], ids[0]]
    if damage == "required-lost":
        value["required_fact_ids"].append(ids[2])
        value["excluded_fact_ids"] = []
    elif damage == "excluded-selected":
        value["excluded_fact_ids"].append(ids[0])
    elif damage == "order":
        value["selected_fact_ids"].reverse()
        value["facts"].reverse()
    elif damage == "reason":
        value["omitted_facts"][0]["reason"] = "artifact-byte-budget"
    elif damage == "omitted-id":
        value["omitted_fact_ids"] = []
    elif damage == "fact-id":
        value["facts"][0]["selected_from"]["fact_id"] = "invented"
    else:
        value["facts"][0]["authority"] = "owner"
    with pytest.raises(CampaignError, match="Final"):
        final.validate_packing(value)


@pytest.mark.parametrize(
    "version,schema_hash,input_hash,expanded_hash",
    [
        (
            "history-final-v4",
            "b3e539da53204d04f429409902c29b0c9561e3ac1fc60d323a811394ed5a80bd",
            "fc9fe400b5816bfb06cb8c2182cead3d33ac962a342bad6d7739251cfef5193d",
            "fccef772a94727e7f23104e5738e3550f42261e4349cacf9aacf19f1d46d2efc",
        ),
        (
            "history-final-v5",
            "c7d0c1b605cb60c8e8558a23f1f4cd65a0178309bdf6e29097bf6d124e5f6e75",
            "7b5ae32177e6f9cf8240646d573125e23b814c97a12826d9ce1c30682b6dcfdb",
            "6702239b3b4dcace8edc252097d10c5a225067c25f1f9da52f5a3838a4790e82",
        ),
    ],
)
def test_historical_v4_v5_schema_input_and_normalized_bytes_unchanged(
    version, schema_hash, input_hash, expanded_hash
):
    # Digests recorded from the released implementation before v6 edits.
    source = {
        "children": [
            {
                "job_id": "child",
                "summary": {
                    "job_id": "child",
                    "structural_coverage": True,
                    "gaps": ["Missing tail"],
                    "facts": [
                        {
                            "id": "one",
                            "kind": "next_action",
                            "text": 'Review 日本語 🧭 \\"',
                            "citations": ["event"],
                            "authority": "context",
                        },
                        {
                            "id": "two",
                            "kind": "progress",
                            "text": "Other review remains pending.",
                            "citations": ["event"],
                            "authority": "context",
                        },
                    ],
                },
            }
        ],
        "owner_quotes": {},
    }
    payload = final.input_payload(
        source, {"path": "source", "sha256": "a" * 64, "job_id": "source"}, version=version
    )
    raw = {
        "schema_version": 2 if version.endswith("v4") else 3,
        "job_id": "job",
        "input_sha256": "b" * 64,
        "covered_ids": ["child"],
        "selected_fact_ids" if version.endswith("v4") else "ranked_fact_ids": list(
            reversed(payload["fact_catalog"])
        ),
    }
    metadata = packing_metadata(1800, raw)
    metadata["complete"] = False
    metadata["output_measurement"]["tokens"] = 12939
    expanded = final.expand(
        raw,
        payload,
        job_id="job",
        input_sha256="b" * 64,
        children=("child",),
        version=version,
        artifact_bytes=1800,
        metadata=metadata,
    )
    assert digest(final.schema_template(version=version)) == schema_hash
    assert digest(payload) == input_hash
    assert digest(expanded) == expanded_hash
