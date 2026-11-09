"""Large portfolio evidence reaches a real bounded stage request without inference."""

import dataclasses
import json
import sys
from pathlib import Path

import pytest
from test_campaign_checkpoint_runtime import checkpoint_case as checkpoint_case
from test_campaign_execution_qualification import writer as writer
from test_campaign_headless import ChildMux
from test_campaign_headless import prepared as prepared
from test_campaign_readonly_qualification import readonly as readonly
from test_campaign_runtime import production_case as production_case
from test_campaign_stage_prompt import large_context

from bmad_loop import campaign_runtime, campaign_workers
from bmad_loop.adapters.profile import get_profile
from bmad_loop.campaign_execution_qualification import qualify
from bmad_loop.campaign_headless import HeadlessRunner, read_json
from bmad_loop.campaign_model import OutboxAction


@pytest.mark.parametrize("mapping", ["unit", "hierarchy"])
def test_confirmed_native_mapping_wins_over_stale_tracker_in_stage_navigation(
    checkpoint_case, monkeypatch, mapping
):
    case = checkpoint_case
    context = large_context()
    case.snapshot.bindings["inventory"] = context["inventory"]
    case.snapshot.bindings.setdefault("source_observations", {})[case.unit.id] = {
        "tracker_id": "https://github.com/foreign/repository/issues/529"
    }
    if mapping == "unit":
        case.unit.bindings["issue"] = {"number": 529}
    else:
        case.unit.bindings.pop("issue", None)
        case.snapshot.bindings["github_hierarchy"] = {
            "story:" + case.unit.id: {"kind": "issue", "number": 529}
        }
    action = OutboxAction(
        case.unit.claim.launch_id,
        "launch",
        case.unit.id,
        {
            "kind": "stage",
            "role": "admission_review",
            "stage": "admission_review",
            "candidate": {"profile": "claude", "model": "fixture"},
            "read_only": True,
            "stage_boundary": {"inputs": {}},
        },
        case.unit.claim.generation,
    )
    captured = []

    class Prepared(Exception):
        pass

    def capture(envelope, request):
        captured.append(request)
        raise Prepared

    monkeypatch.setattr(campaign_runtime, "prepare_launch", capture)
    with pytest.raises(Prepared):
        case.runtime._perform_launch(case.snapshot, action)
    packet = json.loads(captured[0]["prompt"])
    assert any(row.get("number") == 529 for row in packet["context"]["inventory"]["selected"])


@pytest.mark.skipif(sys.platform != "darwin", reason="scripted native read-only qualification")
def test_production_admission_request_keeps_27mb_inventory_out_of_qualified_input(
    checkpoint_case, readonly, tmp_path, monkeypatch
):
    case = checkpoint_case
    # This focused fixture starts before Supervisor.run's first generation.
    # Match that generation for the actual native ownership validator below.
    case.snapshot.generation = 1
    proposal, candidate, cap = readonly
    cap.update(input_tokens=160000, context_tokens=250000)
    proposal.write_text(json.dumps(cap))
    proof = qualify(
        proposal,
        tmp_path / "qualified-reader",
        input_bytes=131072,
        output_tokens=4096,
        timeout_s=20,
        runner=HeadlessRunner(ChildMux()),
    )
    assert proof["ok"], proof  # Scripted CLI and real confinement; zero inference.
    cap = read_json(Path(proof["capability_path"]))
    case.snapshot.config_snapshot["reasoning_capabilities"] = {
        candidate.profile + ":" + candidate.model: {"reasoning": cap}
    }
    template = case.runtime.template(case.snapshot)
    profiles = {
        **template.profiles,
        "campaign:" + candidate.profile: get_profile(candidate.profile),
    }
    monkeypatch.setattr(
        case.runtime, "template", lambda _: dataclasses.replace(template, profiles=profiles)
    )
    context = large_context()
    case.snapshot.bindings["inventory"] = context["inventory"]
    case.unit.spec = dataclasses.replace(
        case.unit.spec, acceptance=tuple(context["unit"]["acceptance"])
    )
    case.unit.bindings.pop("issue", None)
    case.snapshot.bindings.setdefault("source_observations", {})[case.unit.id] = {
        "owner_decisions": context["owner_decisions"],
        "tracker_id": "#529",
    }
    case.unit.claim = dataclasses.replace(
        case.unit.claim,
        launch_id="large-admission",
        generation=case.snapshot.generation,
        role="admission_review",
        run_id="stage-run",
        writes=False,
    )
    action = OutboxAction(
        "large-admission",
        "launch",
        case.unit.id,
        {
            "kind": "stage",
            "role": "admission_review",
            "stage": "admission_review",
            "candidate": dataclasses.asdict(candidate),
            "read_only": True,
            "stage_boundary": {"inputs": {"owner_requirement": "Do not waive acceptance"}},
        },
        case.snapshot.generation,
    )
    captured = []
    prepare = campaign_runtime.prepare_launch

    class Prepared(Exception):
        pass

    def capture(envelope, request):
        path = prepare(envelope, request)
        captured.append((envelope, read_json(path)))
        raise Prepared

    monkeypatch.setattr(campaign_runtime, "prepare_launch", capture)
    with pytest.raises(Prepared):
        case.runtime._perform_launch(case.snapshot, action)
    envelope, request = captured[0]
    assert len(json.dumps(request).encode()) < 512 * 1024
    packet = json.loads(request["prompt"])
    assert packet["context"]["unit"]["acceptance"] == context["unit"]["acceptance"]
    assert packet["context"]["source"]["owner_decisions"] == context["owner_decisions"]
    assert (
        packet["context"]["github_stage_inputs"]["owner_requirement"] == "Do not waive acceptance"
    )
    assert packet["context"]["inventory"]["full_source_required"] is True
    assert any(row.get("number") == 529 for row in packet["context"]["inventory"]["selected"])
    assert packet["context"]["retrievable_context"]["source_size_bytes"] > 27 * 1024 * 1024
    assert request["headless_grants"]["read_only_paths"] == request["context_artifacts"]
    native = []

    def prepared_only(self, path, **kwargs):
        native.append(read_json(path))
        raise Prepared

    monkeypatch.setattr(HeadlessRunner, "launch", prepared_only)
    from bmad_loop.adapters import multiplexer

    class UnusedMux(ChildMux):
        def available(self):
            return True

    # The production fixture's script launcher normally bypasses adapters.
    # Construct the other dormant adapters without probing a real terminal.
    monkeypatch.setattr(multiplexer, "get_multiplexer", lambda: UnusedMux())
    with pytest.raises(Prepared):
        campaign_workers._stage(request, envelope)
    assert len(native) == 1
    actual = native[0]
    assert actual["permissions"] == "read-only-tools"
    assert actual["grants"]["read_only_paths"] == request["context_artifacts"]
    assert actual["input"]["bytes"] < 131072
    assert actual["input"]["token_upper_bound"] < cap["input_tokens"]
    assert actual["request"]["limits"]["input_tokens"] == cap["input_tokens"]
    assert (
        "Return the structured completion result through native JSON output"
        in actual["request"]["prompt"]
    )
    assert actual["request"]["output_schema"]
