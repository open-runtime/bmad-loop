"""Intake coordination must not turn finite product discovery into a full audit."""

import dataclasses

import pytest
from test_campaign_discovery import semantic_inventory as semantic_inventory
from test_campaign_runtime_admission import configured
from test_campaign_runtime_intake import observed

from bmad_loop import campaign_intake_flow as flow
from bmad_loop import campaign_runtime_intake as intake
from bmad_loop.campaign_intake import IntakeBatch
from bmad_loop.campaign_model import CampaignUnit, IntakeSpec, UnitSpec


@pytest.fixture
def scoped_control(semantic_inventory, tmp_path, monkeypatch):
    repo, _, _, _, gateway = semantic_inventory
    project = type("Project", (), {"project": repo})()
    runtime, snapshot, _, _ = configured(project, tmp_path)
    runtime._gateway = gateway
    snapshot.spec = dataclasses.replace(
        snapshot.spec, remote="inventory", intake=IntakeSpec(issues="selected", issue_numbers=(1,))
    )
    product = CampaignUnit(
        UnitSpec("source:project:product", "source", "epic", "Product", scopes=("src/shared.py",))
    )
    snapshot.units[product.id] = product
    snapshot.bindings["inventory"] = {"complete": True, "issues": [], "pulls": []}
    observation = dataclasses.replace(observed(1), repository="org/repo")
    intake.apply_poll(
        snapshot,
        {
            "batch": dataclasses.asdict(
                IntakeBatch((observation,), observation.updated_at, True, 0)
            ),
            "observed_at": 100,
        },
    )
    proposal = flow.next(snapshot)
    assert proposal is not None
    control_id = intake.create_control(snapshot, proposal.payload)
    assert snapshot.units[control_id].spec.scopes == ("resource:campaign-plan",)
    gateway.prs[4] = {**gateway.prs[2], "id": 204, "number": 4, "body": "Unrelated work"}
    monkeypatch.setattr(gateway, "pulls", lambda: [dict(gateway.prs[n]) for n in (2, 4)])
    original_files = gateway.files

    def files(number):
        if number == 4:
            gateway.calls.append(("files", number))
            return [{"filename": "unrelated.txt", "status": "added", "patch": "+unrelated"}]
        return original_files(number)

    monkeypatch.setattr(gateway, "files", files)
    original_reviews = gateway.reviews

    def reviews(number):
        gateway.calls.append(("reviews", number))
        return original_reviews(number)

    monkeypatch.setattr(gateway, "reviews", reviews)
    return runtime, snapshot, gateway, product.id, control_id


def test_controller_created_intake_keeps_finite_product_discovery(scoped_control):
    runtime, snapshot, gateway, _, _ = scoped_control
    result = runtime._perform_inventory(snapshot, None).data
    assert result["complete"], (result["unavailable"], result["truncated"])
    # Global intent and changed-file identities survive, while unrelated detailed
    # review/patch requests do not become necessary just because triage exists.
    assert {row["number"] for row in result["pulls"]} == {2, 4, 9}
    unrelated = next(row for row in result["pulls"] if row["number"] == 4)
    assert unrelated["body"] == "Unrelated work"
    assert ("files", 4) in gateway.calls
    assert ("reviews", 2) in gateway.calls and ("reviews", 9) in gateway.calls
    assert unrelated["discovery_detail_coverage"] == "metadata_only_outside_selected_frontier"
    assert ("reviews", 4) not in gateway.calls


@pytest.mark.parametrize(
    "variant", ["real-planning", "unknown-product", "control-only", "control-path"]
)
def test_genuine_unknown_or_added_product_scope_still_collects_details(scoped_control, variant):
    runtime, snapshot, gateway, product_id, control_id = scoped_control
    control = snapshot.units[control_id]
    if variant == "real-planning":
        control.bindings.pop("intake_control")
        control.spec = dataclasses.replace(control.spec, source="campaign-planning")
    elif variant == "unknown-product":
        snapshot.units[product_id].spec = dataclasses.replace(
            snapshot.units[product_id].spec, scopes=()
        )
    elif variant == "control-only":
        del snapshot.units[product_id]
    else:
        control.spec = dataclasses.replace(control.spec, paths=("unrelated.txt",))
    result = runtime._perform_inventory(snapshot, None).data
    assert result["complete"], (result["unavailable"], result["truncated"])
    assert ("reviews", 4) in gateway.calls
