"""Unknown writer telemetry cannot be laundered through recognized helpers."""

import dataclasses

import pytest
from test_campaign_attribution import finished, session
from test_campaign_publication import publishable as publishable

from bmad_loop.campaign_attribution import development, restore
from bmad_loop.campaign_headless import provider_family
from bmad_loop.campaign_model import UnitState


@pytest.mark.parametrize(
    "model,family",
    [
        ("openai/gpt-6-astra", "openai"),
        ("OpenAI/GPT-6.1-Sol", "openai"),
        ("anthropic/claude-fable-5-1", "anthropic"),
        ("models/gemini-flash", "google"),
        ("google/gemini-flash", "google"),
        ("xai/grok-code", "xai"),
        ("meta/muse-code", "meta"),
        ("anthropic/gpt-6-astra", ""),
        ("openai/claude-fable-5-1", ""),
        ("unknown/gpt-6-astra", ""),
        ("openai/unknown", ""),
        ("meta/unknown", ""),
        ("openai/anthropic/claude-fable-5-1", ""),
        ("/gpt-6-astra", ""),
    ],
)
def test_provider_qualification_requires_matching_known_namespace_and_model(model, family):
    assert provider_family(model) == family
    attributed = development({"sessions": [session("dev", [model])]})
    assert attributed["observed_model"] == model
    assert attributed["contributors"][0]["observed_models"] == [model]
    assert attributed["provider_family"] == family


@pytest.mark.parametrize(
    "models",
    [
        ["unknown-primary", "gpt-helper"],
        ["gpt-primary", "unknown-helper"],
        ["gpt-primary", "anthropic/unknown"],
        ["gpt-primary", ""],
    ],
)
def test_any_unknown_writer_blocks_independent_publication(project, tmp_path, publishable, models):
    _, _, unit = finished(
        project, tmp_path, [session("dev", models), session("review", ["claude-fable-5-1"])]
    )
    assert unit.evidence["developer_attribution_incomplete"]
    assert unit.evidence["developer_families"] == ["openai"]
    controller, candidate, evidence, observation = publishable
    evidence = dataclasses.replace(
        evidence,
        developer_family=unit.evidence["developer_family"],
        developer_families=tuple(unit.evidence["developer_families"]),
        attribution_incomplete=unit.evidence["developer_attribution_incomplete"],
    )
    assert not controller.evaluate(
        candidate, evidence, observation, independent_families=True
    ).ready
    assert controller.evaluate(
        candidate,
        dataclasses.replace(evidence, attribution_incomplete=False),
        observation,
        independent_families=True,
    ).ready


def test_known_provider_qualified_contributors_remain_publishable(project, tmp_path, publishable):
    _, _, unit = finished(
        project,
        tmp_path,
        [
            session("dev", ["openai/gpt-main", "openai/gpt-helper"]),
            session("review", ["unknown-review-helper"]),
        ],
    )
    assert unit.evidence["developer_family"] == "openai"
    assert not unit.evidence["developer_attribution_incomplete"]
    controller, candidate, evidence, observation = publishable
    evidence = dataclasses.replace(
        evidence,
        developer_family=unit.evidence["developer_family"],
        developer_families=tuple(unit.evidence["developer_families"]),
        attribution_incomplete=unit.evidence["developer_attribution_incomplete"],
    )
    assert controller.evaluate(candidate, evidence, observation, independent_families=True).ready


def test_resume_repairs_historical_mixed_unknown_flag_without_rewriting_later_author(
    project, tmp_path
):
    _, snapshot, unit = finished(
        project, tmp_path, [session("dev", ["unknown-primary", "gpt-helper"])]
    )
    unit.state = UnitState.READY_TO_PUBLISH
    unit.evidence["developer_attribution_incomplete"] = False
    unit.evidence["developer_family"] = "anthropic"
    unit.evidence["worker_history"].append(
        {
            "outcome": "stage_complete",
            "result_contract": "repair-v1",
            "provider_family": "anthropic",
        }
    )
    assert restore(snapshot)
    assert unit.evidence["developer_attribution_incomplete"]
    assert unit.evidence["developer_family"] == "anthropic"
    assert not restore(snapshot)


def test_earlier_unknown_contributor_survives_later_known_execution(project, tmp_path):
    _, snapshot, unit = finished(project, tmp_path, [session("dev", ["gpt-main"])])
    unit.evidence["worker_history"].insert(
        0,
        {
            "outcome": "locally_complete",
            "sessions": [session("dev", ["unknown-primary", "gpt-helper"])],
        },
    )
    assert restore(snapshot)
    assert unit.evidence["developer_attribution_incomplete"]
    assert unit.evidence["developer_family"] == "openai"


def _legacy_qualified(snapshot, unit):
    unit.state = UnitState.READY_TO_PUBLISH
    unit.evidence["developer_family"] = ""
    unit.evidence["developer_families"] = []
    unit.evidence["developer_attribution_incomplete"] = True
    prior = unit.evidence["developer_attribution"]
    prior["provider_family"] = ""
    prior["provider_families"] = []
    for row in prior["contributors"]:
        row.pop("complete", None)
        row["provider_families"] = []


@pytest.mark.parametrize("reviewer,kept", [("openai", False), ("anthropic", True)])
def test_exact_legacy_qualified_receipt_recovers_and_rechecks_review(
    project, tmp_path, reviewer, kept
):
    _, snapshot, unit = finished(
        project, tmp_path, [session("dev", ["openai/gpt-main", "openai/gpt-helper"])]
    )
    _legacy_qualified(snapshot, unit)
    unit.evidence["review"] = {"provider_family": reviewer, "launch_id": "old-review"}
    original_models = unit.evidence["developer_attribution"]["contributors"][0]["observed_models"]
    assert restore(snapshot)
    assert unit.evidence["developer_family"] == "openai"
    assert unit.evidence["developer_families"] == ["openai"]
    assert not unit.evidence["developer_attribution_incomplete"]
    assert (
        unit.evidence["developer_attribution"]["contributors"][0]["observed_models"]
        == original_models
    )
    assert ("review" in unit.evidence) is kept
    assert unit.state == (UnitState.READY_TO_PUBLISH if kept else UnitState.SYNCING)
    assert not restore(snapshot)


@pytest.mark.parametrize(
    "uncertainty",
    [
        "unknown-helper",
        "unknown-interrupted-writer",
        "missing-receipt",
        "mismatched-receipt",
        "later-writer",
        "unexplained-family",
    ],
)
def test_legacy_reclassification_never_clears_unproven_writer_debt(project, tmp_path, uncertainty):
    models = ["openai/gpt-main"]
    if uncertainty == "unknown-helper":
        models.append("unknown-helper")
    _, snapshot, unit = finished(project, tmp_path, [session("dev", models)])
    _legacy_qualified(snapshot, unit)
    if uncertainty == "unknown-interrupted-writer":
        unit.evidence["worker_history"].insert(
            0,
            {
                "launch_id": "earlier",
                "outcome": "interrupted",
                "sessions": [session("dev", ["unknown-primary", "gpt-helper"], status="crashed")],
            },
        )
    elif uncertainty == "missing-receipt":
        unit.evidence["worker_history"] = []
    elif uncertainty == "mismatched-receipt":
        unit.evidence["worker_history"][0]["sessions"][0]["models"] = ["openai/gpt-other"]
    elif uncertainty == "later-writer":
        unit.evidence["developer_family"] = "anthropic"
        unit.evidence["worker_history"].append(
            {
                "outcome": "stage_complete",
                "result_contract": "repair-v1",
                "provider_family": "anthropic",
            }
        )
    elif uncertainty == "unexplained-family":
        unit.evidence["developer_families"] = ["meta"]
    restore(snapshot)
    assert unit.evidence["developer_attribution_incomplete"]
    assert unit.evidence["developer_family"] == (
        "anthropic" if uncertainty == "later-writer" else ""
    )


def test_interrupted_unknown_contributor_cannot_be_masked_on_resume(project, tmp_path):
    _, snapshot, unit = finished(project, tmp_path, [session("dev", ["gpt-main"])])
    unit.evidence["worker_history"].insert(
        0,
        {
            "outcome": "interrupted",
            "sessions": [session("dev", ["unknown-primary", "gpt-helper"], status="crashed")],
        },
    )
    assert restore(snapshot)
    assert unit.evidence["developer_attribution_incomplete"]


def test_verified_existing_source_does_not_invent_a_missing_developer(project, tmp_path):
    from bmad_loop.campaign_attribution import record

    _, snapshot, unit = finished(project, tmp_path, [session("dev", ["gpt-main"])])
    unit.evidence.clear()
    receipt = {
        "launch_id": "source-proof",
        "outcome": "existing_verified",
        "source_terminal_receipt": {"no_development_session": True},
    }
    unit.evidence["worker_history"] = [receipt]
    record(snapshot, unit, receipt)
    assert unit.evidence["developer_attribution"]["no_development_session"]
    assert not unit.evidence["developer_attribution_incomplete"]
    # Old persisted receipts lack the explicit attribution fact but retain proof.
    unit.evidence["developer_attribution"].pop("no_development_session")
    assert restore(snapshot)
    assert not unit.evidence["developer_attribution_incomplete"]
    assert not restore(snapshot)


def test_completion_without_developer_observations_keeps_unknown_writer_debt(project, tmp_path):
    from bmad_loop.campaign_attribution import record

    _, snapshot, unit = finished(project, tmp_path, [session("dev", ["gpt-main"])])
    record(
        snapshot, unit, {"launch_id": "unattributed", "outcome": "locally_complete", "sessions": []}
    )
    assert unit.evidence["developer_attribution_incomplete"]
    assert not unit.evidence["developer_attribution"]["no_development_session"]


def test_existing_source_proof_cannot_clear_an_earlier_unknown_writer(project, tmp_path):
    from bmad_loop.campaign_attribution import record

    _, snapshot, unit = finished(
        project, tmp_path, [session("dev", ["unknown-main", "gpt-helper"])]
    )
    receipt = {
        "launch_id": "source-proof",
        "outcome": "existing_verified",
        "source_terminal_receipt": {"no_development_session": True},
    }
    unit.evidence["worker_history"].append(receipt)
    record(snapshot, unit, receipt)
    assert unit.evidence["developer_attribution_incomplete"]
    restore(snapshot)
    assert unit.evidence["developer_attribution_incomplete"]
    assert unit.evidence["developer_families"] == ["openai"]
