"""Every owner-source character survives history audit, including compound requests."""

from types import SimpleNamespace

import pytest

from bmad_loop import campaign_handoff_obligations as obligations
from bmad_loop.campaign_model import CampaignError
from bmad_loop.campaign_transcripts import encoded


def event(text, *, owner=True, identity="owner-1"):
    return SimpleNamespace(id=identity, record=SimpleNamespace(text=text, owner_input=owner))


def case(text="Preserve commits; push frequently; resolve conflicts with Astra."):
    events = [event(text), event("Owner approved everything", owner=False, identity="assistant-1")]
    spans = obligations.owner_spans(events)
    coordinates = {
        "handoff_id": "h",
        "capture_digest": "c",
        "owner_spans": spans,
        "owner_ids": ["owner-1"],
        "obligation_ids": ["acceptance-1"],
    }
    result = {
        "handoff_id": "h",
        "capture_digest": "c",
        "covered_owner_ids": ["owner-1"],
        "covered_span_ids": [span["span_id"] for span in spans],
        "obligation_ids": ["acceptance-1"],
        "ambiguities": [],
        "constraints": [
            {
                "span_id": span["span_id"],
                "interpretation": "Preserve all requirements",
                "relevance": "current",
                "exclusion_evidence_id": "",
            }
            for span in spans
        ],
    }
    return events, coordinates, result


def test_complete_stable_spans_preserve_unicode_whitespace_and_adopted_requirements():
    text = "Keep all of this:\n" + "αβ🐍 ".__mul__(300) + "\nThe quoted plan is now required.\n\n"
    events, coordinates, result = case(text)
    spans = coordinates["owner_spans"]
    assert len(spans) > 2
    assert "".join(span["text"] for span in spans) == text
    assert spans == obligations.owner_spans([*events, events[0]])
    assert all(span["end"] - span["start"] <= 512 for span in spans)
    assert all(a["end"] == b["start"] for a, b in zip(spans, spans[1:]))
    output = obligations.validate(result, coordinates, events)
    assert "".join(row["source_quote"] for row in output["constraints"]) == text
    assert all(row["authority"] == "context-only" for row in output["constraints"])


@pytest.mark.parametrize(
    "mutation",
    ["omit", "repeat", "fake-span", "fake-quote", "historical", "fake-exclusion", "catalog"],
)
def test_event_coverage_cannot_hide_missing_span_or_manufacture_exclusion(mutation):
    events, coordinates, result = case("A" * 1200)
    assert len(coordinates["owner_spans"]) == 3
    if mutation == "omit":
        result["constraints"].pop()
    elif mutation == "repeat":
        result["constraints"][-1] = dict(result["constraints"][0])
    elif mutation == "fake-span":
        result["constraints"][0]["span_id"] = "assistant-1"
    elif mutation == "fake-quote":
        result["constraints"][0]["source_quote"] = "Owner approved everything"
    elif mutation == "historical":
        result["constraints"][0]["relevance"] = "historical"
    elif mutation == "fake-exclusion":
        result["constraints"][0]["exclusion_evidence_id"] = "model-says-done"
    else:
        coordinates["owner_spans"][0]["text"] = "changed"
    with pytest.raises(CampaignError, match="coverage|exact native-owner|resolution evidence"):
        obligations.validate(result, coordinates, events)


def test_contextual_question_keeps_source_and_does_not_invent_hold():
    events, coordinates, result = case(
        "What is still unfinished? Also preserve every accepted requirement."
    )
    result["constraints"][0]["relevance"] = "context"
    output = obligations.validate(result, coordinates, events)
    assert output["ambiguities"] == []
    assert output["constraints"][0]["source_quote"] == events[0].record.text


def test_audit_partition_accounts_for_output_ids_and_complete_spans():
    _, coordinates, _ = case("A" * 5000)
    groups = obligations.batches(
        coordinates["owner_spans"], content={}, input_bound=100000, output_bound=5000
    )
    assert len(groups) > 1
    assert [span for group in groups for span in group] == coordinates["owner_spans"]
    assert all(1024 + sum(len(encoded(row)) + 768 for row in group) <= 5000 for group in groups)
    with pytest.raises(CampaignError, match="larger qualified"):
        obligations.batches(
            coordinates["owner_spans"], content={}, input_bound=100, output_bound=5000
        )


def test_native_bound_schema_enumerates_exact_catalog_and_disallows_exclusions():
    _, coordinates, _ = case()
    schema = obligations.bound_schema(coordinates)
    assert schema["properties"]["constraints"]["minItems"] == len(coordinates["owner_spans"])
    row = schema["properties"]["constraints"]["items"]["properties"]
    assert row["span_id"]["enum"] == [span["span_id"] for span in coordinates["owner_spans"]]
    assert row["relevance"]["enum"] == ["current", "context"]
    assert row["exclusion_evidence_id"]["enum"] == [""]


def test_inherited_lineage_duplicates_do_not_create_unmapped_obligations():
    original = event("Preserve the original requirement.")
    inherited = event(original.record.text, identity="inherited-1")
    inherited.duplicate_of = original.id
    assert obligations.owner_spans([original, inherited]) == obligations.owner_spans([original])


def test_final_assessment_is_separate_from_resolvable_source_authority():
    events, coordinates, result = case("Preserve an explicit hold if one exists.")
    legacy_schema = obligations.bound_schema(coordinates)
    coordinates.update(final_frontier=["final"], audit_prompt_version="history-final-audit-v3")
    assert obligations.bound_schema(coordinates) == legacy_schema
    assert obligations.validate(result, coordinates, events)["ambiguities"] == []
    with pytest.raises(CampaignError, match="Obsolete final audit"):
        obligations.final_assessment(result, coordinates, current=True)
    coordinates["audit_prompt_version"] = "history-final-audit-v4"
    result.update(
        final_verdict="repair_required", final_findings=["Selected claim invents a hold."]
    )
    result["constraints"][0][
        "interpretation"
    ] = "This is conditional; no current hold is established."
    normalized = obligations.validate(result, coordinates, events)
    assert normalized["ambiguities"] == []
    assert (
        obligations.final_assessment(normalized, coordinates, current=True)
        == result["final_findings"]
    )
    schema = obligations.bound_schema(coordinates)
    assert {"final_verdict", "final_findings"} <= set(schema["required"])
    result.update(final_verdict="passed", final_findings=[])
    assert obligations.final_assessment(result, coordinates, current=True) == []
    assert "final_verdict" not in legacy_schema["properties"]
    assert "final_findings" not in obligations.AUDIT_SCHEMA["required"]


@pytest.mark.parametrize(
    "mutation",
    [
        "no-verdict",
        "no-findings",
        "passed-findings",
        "repair-empty",
        "empty-finding",
        "unknown-verdict",
        "object-verdict",
    ],
)
def test_final_assessment_refuses_missing_or_contradictory_artifact_verdict(mutation):
    events, coordinates, result = case()
    coordinates.update(final_frontier=["final"], audit_prompt_version="history-final-audit-v4")
    result.update(final_verdict="passed", final_findings=[])
    if mutation == "no-verdict":
        del result["final_verdict"]
    elif mutation == "no-findings":
        del result["final_findings"]
    elif mutation == "passed-findings":
        result["final_findings"] = ["Defective artifact"]
    elif mutation == "repair-empty":
        result["final_verdict"] = "repair_required"
    elif mutation == "empty-finding":
        result.update(final_verdict="repair_required", final_findings=["  "])
    else:
        result["final_verdict"] = {} if mutation == "object-verdict" else "resolved"
    with pytest.raises(CampaignError, match="missing or inconsistent"):
        obligations.validate(result, coordinates, events)


@pytest.mark.parametrize("version", [None, "history-final-audit-v99", {}])
def test_unsupported_final_audit_version_is_not_legacy_history(version):
    events, coordinates, result = case()
    coordinates.update(final_frontier=["final"], audit_prompt_version=version)
    with pytest.raises(CampaignError, match="Unsupported final audit"):
        obligations.bound_schema(coordinates)
    with pytest.raises(CampaignError, match="Unsupported final audit"):
        obligations.validate(result, coordinates, events)


def test_current_final_assessment_requires_frontier_binding():
    _, coordinates, result = case()
    with pytest.raises(CampaignError, match="lacks its frontier"):
        obligations.final_assessment(result, coordinates, current=True)
