"""A copied checklist cannot qualify another native route or inflate its limits."""

import copy
from dataclasses import replace
from pathlib import Path

import pytest
from test_campaign_headless import prepared as prepared

from bmad_loop.campaign_headless import file_digest, read_json, validate_capability, write_json
from bmad_loop.campaign_model import CampaignError


@pytest.mark.parametrize(
    "change", ["effort", "argv", "permissions", "input", "output", "turns", "checks"]
)
def test_copied_capability_cannot_claim_unearned_route_or_bounds(prepared, change):
    _, _, _, candidate, original = prepared
    validate_capability(original, candidate)
    cap = copy.deepcopy(original)
    if change == "effort":
        cap["efforts"] = ["medium"]
        candidate = replace(candidate, effort="medium")
    elif change == "argv":
        cap["argv"].append("--arbitrary-new-mode")
    elif change == "permissions":
        cap["permissions"] = "workspace-tools"
        cap["argv"].append("{grants_path}")
        cap["qualification"]["checks"].remove("no_tools")
        cap["qualification"]["checks"].append("scoped_tools")
    elif change in {"input", "output"}:
        key = change + "_tokens"
        cap[key] += 1000
        cap["qualification"][key] += 1000
    elif change == "turns":
        cap["max_turns"] += 1
    else:
        cap["qualification"]["checks"].append("invented_coverage")
    with pytest.raises(CampaignError, match="qualification"):
        validate_capability(cap, candidate, permissions=cap["permissions"])


def test_earned_capability_may_narrow_numeric_admission_bounds(prepared):
    _, _, _, candidate, cap = prepared
    narrowed = {**cap, "input_tokens": cap["input_tokens"] // 2, "output_tokens": 128}
    validate_capability(narrowed, candidate)


def test_report_hash_alone_cannot_turn_fabricated_coverage_into_proof(prepared):
    _, _, _, candidate, cap = prepared
    cap = copy.deepcopy(cap)
    path = Path(cap["qualification"]["path"])
    report = read_json(path)
    report["coverage"]["input_tokens"] += 1000
    write_json(path, report)
    cap["qualification"]["sha256"] = file_digest(path)
    cap["qualification"]["input_tokens"] += 1000
    cap["input_tokens"] += 1000
    with pytest.raises(CampaignError, match="earned evidence"):
        validate_capability(cap, candidate)


def test_version2_evidence_binds_the_entire_immutable_request_file(prepared):
    _, _, _, candidate, cap = prepared
    path = Path(cap["qualification"]["path"]).parent / "request.json"
    request = read_json(path)
    request["capability"]["argv"].append("--changed")
    write_json(path, request)
    with pytest.raises(CampaignError, match="request binding changed"):
        validate_capability(cap, candidate)


def test_legacy_report_requires_exact_frozen_request_and_owned_receipt(prepared):
    _, _, _, candidate, cap = prepared
    cap = copy.deepcopy(cap)
    path = Path(cap["qualification"]["path"])
    report = read_json(path)
    report["schema_version"] = 1
    report.pop("request_binding")
    write_json(path, report)
    cap["qualification"]["sha256"] = file_digest(path)
    validate_capability(cap, candidate)
    receipt_path = path.parent / "receipt.json"
    receipt = read_json(receipt_path)
    receipt["launch_id"] = "another-owned-launch"
    write_json(receipt_path, receipt)
    with pytest.raises(CampaignError, match="lifecycle evidence"):
        validate_capability(cap, candidate)
