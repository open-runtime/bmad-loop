"""The native pair reaches ordinary child composition with earned zero-token proof."""

import dataclasses
import sys
from pathlib import Path

import pytest
from conftest import git, install_bmad_config
from test_campaign_codex_review import qualify_recipe
from test_campaign_execution_qualification import writer as writer
from test_campaign_headless import prepared as prepared
from test_campaign_native_review_flow import configured
from test_campaign_readonly_qualification import readonly as readonly
from test_campaign_workers import envelope, worker_request

from bmad_loop import campaign_workers as workers
from bmad_loop.adapters import multiplexer
from bmad_loop.campaign_headless import read_json
from bmad_loop.campaign_model import CampaignError, OutboxAction
from bmad_loop.campaign_planning import StageCoordinates, stage_packet
from bmad_loop.policy import AdapterPolicy, Policy


@pytest.mark.skipif(sys.platform != "darwin", reason="earned native read-only sandbox capability")
def test_actual_child_pair_is_validated_and_retained_before_independent_review(
    project, tmp_path, monkeypatch, prepared, readonly
):
    runner, *_ = prepared
    proof, candidate = qualify_recipe(readonly, tmp_path, input_bytes=16384)
    assert proof["ok"], proof
    cap = read_json(Path(proof["capability_path"]))
    install_bmad_config(project)
    runtime, snapshot, unit = configured(project, tmp_path)
    base = unit.bindings["head"]
    (project.project / "candidate.txt").write_text("candidate after accepted main\n")
    git(project.project, "add", "candidate.txt")
    git(project.project, "add", "-f", "_bmad/bmm/config.yaml")
    git(project.project, "commit", "-qm", "candidate")
    head = git(project.project, "rev-parse", "HEAD").strip()
    unit.bindings.update(head=head, base=base)
    unit.evidence["integrated"] = {"head": head, "base": base}
    coordinates = StageCoordinates(
        snapshot.id, unit.id, "native_review_pair", 1, 1, base, head, "inventory"
    )
    policy = Policy(
        adapter=AdapterPolicy(name="codex", model=candidate.model, effort=candidate.effort)
    )
    context = dataclasses.replace(
        envelope(project.project, tmp_path / "controller", policy),
        campaign_id=snapshot.id,
        unit_id=unit.id,
        generation=snapshot.generation,
        config_revision=snapshot.config_revision,
    )
    request = worker_request(
        project.project,
        context,
        kind="stage",
        role="native_review_pair",
        stage="native_review_pair",
        candidate=dataclasses.asdict(candidate),
        profile_key="triage",
        read_only=True,
        coordinates=dataclasses.asdict(coordinates),
        headless_coordinates=dataclasses.asdict(coordinates),
        headless_capabilities={candidate.profile + ":" + candidate.model: {"review": cap}},
        headless_grants={"workspace": str(project.project), "generation": snapshot.generation},
        prompt=stage_packet(coordinates, context={}),
        timeout_s=30,
    )
    path = workers.prepare_launch(context, request)
    frozen = read_json(path)
    monkeypatch.setattr(multiplexer, "get_multiplexer", lambda: runner.mux)
    monkeypatch.setattr(multiplexer, "mux_usable", lambda mux: True)
    assert workers.child_main(path) == 0, read_json(context.directory / "receipt.json")
    receipt = read_json(context.directory / "receipt.json")
    assert receipt["outcome"] == "stage_complete", receipt
    runtime._validate_stage_receipt(snapshot, unit, frozen, receipt)
    without_native_binding = dict(receipt)
    without_native_binding.pop("native_binding")
    with pytest.raises(CampaignError, match="native"):
        runtime._validate_stage_receipt(snapshot, unit, frozen, without_native_binding)
    evidence = receipt["validated_native_review_pair"]
    assert evidence["target"] == {"head": head, "base": base}
    assert [row["kind"] for row in evidence["native_review_evidence"]["reviews"]] == [
        "main",
        "custom",
    ]
    assert git(project.project, "rev-parse", "HEAD").strip() == head
    assert not git(project.project, "status", "--porcelain").strip()
    snapshot.outbox.append(
        OutboxAction(frozen["launch_id"], "launch", unit.id, request, snapshot.generation)
    )
    monkeypatch.setattr(runtime, "_launcher_readiness", lambda *args, **kwargs: {"ready": True})
    runtime.control(
        snapshot, {"kind": "prepare-effect", "action": dataclasses.asdict(snapshot.outbox[-1])}
    )
    assert unit.claim.resource == "review"
    assert not unit.claim.writes
    unit.claim.quiescent = True
    runtime._apply_stage_result(snapshot, unit, receipt)
    assert unit.evidence["native_review_pair"]["validated_native_review_pair"] == evidence
    # Confirm the launch intent before considering the next stage in this seam.
    snapshot.outbox[-1].state = "confirmed"
    next_step = next(row for row in runtime.proposals(snapshot) if row.unit == unit.id)
    assert next_step.payload["launch"]["stage"] == "publication_review"
