"""Closed final selections retain attribution through bounded successor context."""

import copy
import dataclasses
import json
from pathlib import Path

import pytest
from test_campaign_handoff_context import budget, capture, native_row, quotes, result

from bmad_loop import campaign_handoff_context as context
from bmad_loop import campaign_summary_final as final_selection
from bmad_loop.campaign_model import CampaignError
from bmad_loop.campaign_summary import plan_maps, validate_summary
from bmad_loop.campaign_transcripts import blob, encoded, load_events


def closed_packet(
    tmp_path,
    *,
    mutate=None,
    include_owner=True,
    historical_text=None,
    historical_facts=None,
    selection_order=None,
    required_indices=(),
    excluded_indices=(),
    version="history-final-v4",
    current_state=None,
):
    history = capture(tmp_path, [native_row("codex")])
    events = load_events(history)
    child_spec = plan_maps(events, budget(), tmp_path / "maps")[0]
    raw = result(child_spec, events)
    if not include_owner:
        raw["facts"] = [
            {
                "kind": "next_action",
                "text": historical_text or 'Leave the historical "review" pending.\nContext only.',
                "citations": [events[0].id],
            }
        ]
    if historical_facts:
        raw["facts"] = [
            {"kind": kind, "text": text, "citations": [events[0].id]}
            for kind, text in historical_facts
        ]
    child = validate_summary(child_spec, raw, events, observed_output_tokens=1000)
    original_input = {
        "children": [{"job_id": child_spec.id, "summary": child}],
        "owner_quotes": quotes(history, child),
    }
    original_path = blob(tmp_path / "final-input", encoded(original_input))
    projected = final_selection.input_payload(
        original_input,
        {"path": str(original_path), "sha256": original_path.stem, "job_id": "closed-final"},
        version=version,
    )
    path = blob(tmp_path / "final-input", encoded(projected))
    field = "selected_fact_ids" if version == "history-final-v4" else "ranked_fact_ids"
    selection = {
        "schema_version": {"history-final-v4": 2, "history-final-v5": 3, "history-final-v6": 4}[
            version
        ],
        "job_id": "closed-final",
        "input_sha256": path.stem,
        "covered_ids": [child_spec.id],
        field: list(projected["fact_catalog"]),
    }
    if version == "history-final-v6":
        ids = selection[field]
        selection["required_fact_ids"] = [ids[i] for i in required_indices]
        selection["excluded_fact_ids"] = [ids[i] for i in excluded_indices]
    if selection_order is not None:
        selected = selection[field]
        selection[field] = [selected[index] for index in selection_order]
    final = {
        **child,
        **final_selection.expand(
            selection,
            projected,
            job_id="closed-final",
            input_sha256=path.stem,
            children=(child_spec.id,),
            version=version,
        ),
    }
    final_spec = dataclasses.replace(
        child_spec,
        id=final["job_id"],
        purpose="handoff-reduce",
        input_path=str(path),
        input_sha256=path.stem,
        prompt_version=version,
        primary_ids=(child_spec.id,),
        dependencies=(child_spec.id,),
    )
    if mutate:
        mutate(final)
    packet = context.assemble_packet(
        (history,),
        ({"id": "canonical", "criterion": "Preserve existing commits."},),
        {"pr": 17, "hold": None} if current_state is None else current_state,
        (final,),
        dataclasses.replace(budget(), context_tokens=80000),
        tmp_path / "packet",
        summary_tree=((child_spec, child), (final_spec, final)),
    )
    payload = json.loads(Path(packet.path).read_text())
    payload["resolved_constraints"] = [
        {"event_id": events[0].id, "source_quote": events[0].record.text}
    ]
    return payload, final, child


@pytest.mark.parametrize("version", ["history-final-v4", "history-final-v5", "history-final-v6"])
def test_bounded_closed_history_retains_exact_attribution_and_owner_provenance(tmp_path, version):
    payload, final, _ = closed_packet(tmp_path, version=version)
    context.fit_audited_history(payload, tmp_path / "packet", ceiling=20000)
    view = payload["history_view"][0]
    original = final["facts"][0]
    assert view["text"] == original["text"]
    assert view["final_contract"] == version
    for key in ("selected_from", "citations", "source_quote", "source_quote_ref"):
        assert view[key] == original[key]
    assert view["authority"] == "context"
    assert context.retrieve_history_fact(payload, final["job_id"], original["id"]) == original


def test_selected_historical_instruction_cannot_replace_mandatory_current_authority(tmp_path):
    payload, final, _ = closed_packet(tmp_path, include_owner=False)
    mandatory = context.mandatory_context_digest(payload)
    context.fit_audited_history(payload, tmp_path / "packet", ceiling=20000)
    assert context.mandatory_context_digest(payload) == mandatory
    assert payload["current_state"] == {"pr": 17, "hold": None}
    assert payload["obligations"] == [
        {"id": "canonical", "criterion": "Preserve existing commits."}
    ]
    assert payload["resolved_constraints"][0]["source_quote"] == "keep all commits"
    assert payload["history_view"][0]["text"] == final["facts"][0]["text"]
    assert payload["history_view"][0]["text"].startswith("Historical summary claim from child ")
    owners = json.loads(Path(payload["owner_index"]["path"]).read_text())
    assert owners[0]["quote"] == "keep all commits"


def test_v6_required_history_does_not_need_to_duplicate_mandatory_controller_hold(tmp_path):
    from bmad_loop.campaign_transcripts import digest

    state = {
        "active_hold": {"id": "controller-only-hold", "reason": "Wait for explicit resolution"}
    }
    current = {
        "controller_controls": {
            "observed_revision": 7,
            "state": state,
            "state_digest": digest(state),
        }
    }
    payload, final, child = closed_packet(
        tmp_path,
        version="history-final-v6",
        include_owner=False,
        historical_text="Implementation has a pending review.",
        required_indices=(0,),
        current_state=current,
    )
    assert "controller-only-hold" not in json.dumps(child)
    assert "controller-only-hold" not in json.dumps(final)
    context.bind_authority_evidence(payload, tmp_path / "packet", [])
    mandatory = context.mandatory_context_digest(payload)
    context.fit_audited_history(payload, tmp_path / "packet", ceiling=20000)
    assert payload["current_state"] == current
    assert payload["history_view"][0]["required_context"] is True
    assert "pending review" in payload["history_view"][0]["text"]
    context.validate_authority_evidence(payload, mandatory)
    context.validate_history_view(payload)
    payload["current_state"]["controller_controls"]["state"]["active_hold"] = None
    with pytest.raises(CampaignError, match="mandatory context differs"):
        context.validate_authority_evidence(payload, mandatory)


@pytest.mark.parametrize(
    "fault", ["missing-attribution", "missing-contract", "wrong-contract", "bare-instruction"]
)
@pytest.mark.parametrize("version", ["history-final-v4", "history-final-v5", "history-final-v6"])
def test_closed_parent_artifact_cannot_silently_downgrade_to_legacy_context(
    tmp_path, fault, version
):
    def mutate(final):
        if fault == "missing-attribution":
            final["facts"][0].pop("selected_from")
        elif fault == "missing-contract":
            final.pop("final_contract")
        elif fault == "wrong-contract":
            final["final_contract"] = (
                "history-final-v5" if version == "history-final-v4" else "history-final-v4"
            )
        else:
            final["facts"][0]["text"] = "Stop all review."

    with pytest.raises(CampaignError):
        closed_packet(tmp_path, mutate=mutate, version=version)


@pytest.mark.parametrize("key", ["selected_from", "citations", "source_quote", "source_quote_ref"])
def test_v4_bounded_view_cannot_drop_retained_attribution_or_source_fields(tmp_path, key):
    payload, _, _ = closed_packet(tmp_path)
    context.fit_audited_history(payload, tmp_path / "packet", ceiling=20000)
    payload["history_view"][0].pop(key)
    with pytest.raises(CampaignError, match="immutable parent fact"):
        context.validate_history_view(payload)


@pytest.mark.parametrize("version", ["history-final-v4", "history-final-v5", "history-final-v6"])
def test_closed_attribution_and_unicode_are_charged_to_exact_packet_boundary(tmp_path, version):
    payload, _, _ = closed_packet(
        tmp_path,
        include_owner=False,
        historical_text='Proposed: inspect "条件"\nthen verify.',
        version=version,
    )
    fitted = copy.deepcopy(payload)
    context.fit_audited_history(fitted, tmp_path / "packet", ceiling=20000)
    exact = len(encoded(fitted))
    same = copy.deepcopy(payload)
    context.fit_audited_history(same, tmp_path / "packet", ceiling=exact)
    assert len(encoded(same)) == exact
    assert same["history_view"] == fitted["history_view"]
    with pytest.raises(context.HistoryContextRecoveryNeeded, match="useful complete"):
        context.fit_audited_history(copy.deepcopy(payload), tmp_path / "packet", ceiling=exact - 1)


@pytest.mark.parametrize("version", ["history-final-v4", "history-final-v5", "history-final-v6"])
def test_closed_selected_order_survives_kind_priority_and_bounded_omission(tmp_path, version):
    payload, final, _ = closed_packet(
        tmp_path,
        historical_facts=[
            ("next_action", "First historical proposal."),
            ("progress", 'Observed "条件" before acting.'),
            ("next_action", "Another historical proposal."),
            ("failure", "The verification failed."),
        ],
        selection_order=(1, 2, 0, 3),
        version=version,
    )
    mandatory = context.mandatory_context_digest(payload)
    fitted = copy.deepcopy(payload)
    context.fit_audited_history(fitted, tmp_path / "packet", ceiling=30000)
    expected = [fact["selected_from"]["catalog_id"] for fact in final["facts"]]
    assert [row["selected_from"]["catalog_id"] for row in fitted["history_view"]] == expected

    two_facts = copy.deepcopy(fitted)
    two_facts["history_view"] = two_facts["history_view"][:2]
    two_facts["omitted_history_fact_index"]["count"] = 2
    ceiling = len(encoded(two_facts))  # Digest paths retain fixed serialized width.
    bounded = copy.deepcopy(payload)
    context.fit_audited_history(bounded, tmp_path / "packet", ceiling=ceiling)
    assert len(encoded(bounded)) == ceiling
    assert [row["selected_from"]["catalog_id"] for row in bounded["history_view"]] == expected[:2]
    assert context.mandatory_context_digest(bounded) == mandatory
    omitted = json.loads(Path(bounded["omitted_history_fact_index"]["path"]).read_text())
    assert [row["fact_id"] for row in omitted["omitted"]] == [
        fact["id"] for fact in final["facts"][2:]
    ]
    for fact in final["facts"]:
        assert context.retrieve_history_fact(bounded, final["job_id"], fact["id"]) == fact


def test_legacy_fact_projection_keeps_its_original_shape(tmp_path):
    from test_campaign_handoff_view import history_packet

    payload, full = history_packet(tmp_path)
    context.fit_audited_history(payload, tmp_path / "packet", ceiling=10000)
    view = payload["history_view"][0]
    assert set(view) == {
        "summary_id",
        "summary_sha256",
        "fact_id",
        "kind",
        "text",
        "authority",
        "semantic_fidelity",
    }
    assert (
        context.retrieve_history_fact(payload, full["job_id"], view["fact_id"]) == full["facts"][0]
    )


def test_legacy_initial_history_keeps_reverse_order_within_kind(tmp_path):
    from test_campaign_handoff_view import history_packet

    payload, full = history_packet(tmp_path, fact_count=3)
    context.fit_audited_history(payload, tmp_path / "packet", ceiling=15000)
    assert [row["fact_id"] for row in payload["history_view"]] == [
        fact["id"] for fact in reversed(full["facts"])
    ]


def test_required_review_survives_second_context_packing_and_excluded_advice_stays_out(tmp_path):
    payload, final, _ = closed_packet(
        tmp_path,
        historical_facts=[
            ("next_action", "Do not merge, even though no active hold is established."),
            ("nuance", "Old background detail. " * 40),
            ("verification_claim", "Claim 1 is unverified; its review remains pending."),
        ],
        version="history-final-v6",
        required_indices=(2,),
        excluded_indices=(0,),
    )
    complete = copy.deepcopy(payload)
    context.fit_audited_history(complete, tmp_path / "packet", ceiling=30000)
    assert len(complete["history_view"]) == 2
    required = complete["history_view"][0]
    assert required["required_context"] is True
    assert "review remains pending" in required["text"]
    assert all("Do not merge" not in row["text"] for row in complete["history_view"])
    assert final["omitted_facts"] == [
        {"catalog_id": final["excluded_fact_ids"][0], "reason": "model-excluded-context"}
    ]
    only_required = copy.deepcopy(complete)
    only_required["history_view"] = [required]
    only_required["omitted_history_fact_index"]["count"] = 1
    ceiling = len(encoded(only_required))
    bounded = copy.deepcopy(payload)
    context.fit_audited_history(bounded, tmp_path / "packet", ceiling=ceiling)
    assert bounded["history_view"] == [required]
    assert len(encoded(bounded)) == ceiling
    with pytest.raises(context.HistoryContextRecoveryNeeded, match="Required historical context"):
        context.fit_audited_history(
            copy.deepcopy(payload), tmp_path / "packet", ceiling=ceiling - 1
        )


def test_launch_validation_refuses_missing_required_fact_even_with_consistent_omission_index(
    tmp_path,
):
    payload, _, _ = closed_packet(
        tmp_path,
        historical_facts=[("progress", "Review is still pending."), ("nuance", "Background.")],
        version="history-final-v6",
        required_indices=(0,),
    )
    context.fit_audited_history(payload, tmp_path / "packet", ceiling=30000)
    payload["history_view"] = [
        row for row in payload["history_view"] if not row["required_context"]
    ]
    # Keep the independent omission index internally consistent: only the
    # required-context invariant should reject this otherwise useful packet.
    facts = context._history_facts(payload)
    omitted = context._fact_omissions(facts, payload["history_view"])
    path = blob(tmp_path / "omitted", encoded(omitted))
    payload["omitted_history_fact_index"] = {
        "path": str(path),
        "sha256": path.stem,
        "count": len(omitted["omitted"]),
    }
    with pytest.raises(
        context.HistoryContextRecoveryNeeded, match="Required historical context is missing"
    ):
        context.validate_history_view(payload)


def test_optional_fact_fitting_does_not_hide_oversized_required_context(tmp_path):
    payload, _, _ = closed_packet(
        tmp_path,
        historical_facts=[
            ("verification_claim", "Unfinished review detail. " * 80),
            ("nuance", "Small optional background."),
        ],
        version="history-final-v6",
        required_indices=(0,),
    )
    full = copy.deepcopy(payload)
    context.fit_audited_history(full, tmp_path / "packet", ceiling=30000)
    assert len(full["history_view"]) == 2
    full["history_view"] = [row for row in full["history_view"] if not row["required_context"]]
    full["omitted_history_fact_index"]["count"] = 1
    # The base envelope and one useful optional fact fit. Required context alone
    # must cause refusal, independent of the general nonempty-history check.
    ceiling = len(encoded(full))
    with pytest.raises(context.HistoryContextRecoveryNeeded, match="Required historical context"):
        context.fit_audited_history(payload, tmp_path / "packet", ceiling=ceiling)
