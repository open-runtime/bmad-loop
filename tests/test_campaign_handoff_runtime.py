"""Production supervision, real child PIDs, scripted zero-token model responses."""

import copy
import dataclasses
import json
import time
from contextlib import contextmanager, nullcontext

import pytest
from test_campaign_handoff_flow import campaign as campaign
from test_campaign_headless import prepared as prepared
from test_campaign_runtime import diagnose
from test_campaign_runtime import production_case as production_case

from bmad_loop.campaign_headless import HeadlessRunner, read_json
from bmad_loop.campaign_summary import SummaryJobSpec, load_input, quote_catalog
from bmad_loop.campaign_supervisor import CampaignSupervisor


class ScriptedReasoning:
    """Only model execution is scripted; preparation and receipt validation are real."""

    def __init__(self):
        self.prepare = HeadlessRunner().prepare
        self.completed = {}

    def launch(self, path, *, admission=None):
        if path not in self.completed:
            with admission() if admission is not None else nullcontext():
                observed = self._launch(path)
            self.completed[path] = copy.deepcopy(observed)
        return self.observe(path)

    def _launch(self, path):
        envelope = read_json(path)
        request = envelope["request"]
        coordinate = request["coordinates"]
        if request["purpose"] == "handoff_map":
            spec = SummaryJobSpec(**coordinate["summary_spec"])
            source = load_input(spec)
            result = {
                "schema_version": 1,
                "job_id": request["id"],
                "input_sha256": spec.input_sha256,
                "covered_ids": list(spec.primary_ids),
                "gaps": [],
                "facts": [
                    {
                        "kind": "owner_request",
                        "text": row["text"],
                        "source_quote_id": next(
                            key
                            for key, value in quote_catalog(spec).items()
                            if value["event_id"] == row["event_id"]
                        ),
                        "citations": [row["event_id"]],
                    }
                    for row in source["primary"]
                    if row["owner_input"]
                ],
            }
        else:
            assert request["purpose"] == "handoff_audit"
            result = {
                "handoff_id": coordinate["handoff_id"],
                "capture_digest": coordinate["capture_digest"],
                "covered_owner_ids": coordinate["owner_ids"],
                "covered_span_ids": [span["span_id"] for span in coordinate["owner_spans"]],
                "obligation_ids": coordinate["obligation_ids"],
                "ambiguities": [],
                **(
                    {"final_verdict": "passed", "final_findings": []}
                    if "final_verdict" in request["output_schema"].get("required", ())
                    else {}
                ),
                "constraints": [
                    {
                        "span_id": span["span_id"],
                        "interpretation": span["text"],
                        "relevance": "current",
                        "exclusion_evidence_id": "",
                    }
                    for span in coordinate["owner_spans"]
                ],
            }
        return {
            "state": "finished",
            "receipt": {
                "job_id": request["id"],
                "request_digest": envelope["request_digest"],
                "launch_id": envelope["launch_id"],
                "generation": envelope["generation"],
                "quiescent": True,
                "outcome": "completed",
                "input_complete": True,
                "usage": {},
                "result": result,
                "observed_model": envelope["selection"]["model"],
                "provider_family": (
                    "anthropic" if request["purpose"] == "handoff_audit" else "openai"
                ),
            },
        }

    def observe(self, path):
        if path not in self.completed:
            return {"state": "unknown", "reason": "No admitted scripted launch completed"}
        return copy.deepcopy(self.completed[path])


@pytest.fixture
def scripted_reasoning(tmp_path, prepared, monkeypatch, request):
    from bmad_loop.campaign_fidelity import FINAL_AUDIT_PROMPT_VERSION
    from bmad_loop.campaign_handoff_obligations import bound_schema

    variant = getattr(request, "param", "history")
    _, _, original, candidate, capability = prepared
    coordinates = {
        **original.coordinates,
        "handoff_id": "handoff",
        "capture_digest": "capture",
        "owner_ids": [],
        "owner_spans": [],
        "obligation_ids": [],
        **(
            {
                "final_frontier": ["final-fixture"],
                "audit_prompt_version": (
                    "history-final-audit-v3"
                    if variant == "legacy-final"
                    else FINAL_AUDIT_PROMPT_VERSION
                ),
            }
            if variant != "history"
            else {}
        ),
    }
    request = dataclasses.replace(
        original,
        purpose="handoff_audit",
        role="handoff_audit",
        coordinates=coordinates,
        output_schema=bound_schema(coordinates),
    )
    runner = ScriptedReasoning()
    path = runner.prepare(
        tmp_path / "scripted",
        request,
        candidate,
        capability,
        launch_id="scripted",
        generation=1,
    )
    launches = []
    original = runner._launch

    def launched(path):
        launches.append(path)
        return original(path)

    monkeypatch.setattr(runner, "_launch", launched)
    return runner, path, launches


@pytest.mark.parametrize(
    "scripted_reasoning", ["history", "legacy-final", "current-final"], indirect=True
)
def test_scripted_history_audit_satisfies_current_contract(scripted_reasoning):
    from bmad_loop.campaign_handoff_obligations import final_assessment, validate
    from bmad_loop.campaign_jobs import validate_result

    runner, path, _ = scripted_reasoning
    request = read_json(path)["request"]
    result = runner.launch(path)["receipt"]["result"]
    validate_result(result, request["output_schema"])
    validate(result, request["coordinates"], [])
    if request["coordinates"].get("final_frontier"):
        assert (
            final_assessment(
                result,
                request["coordinates"],
                current="final_verdict" in request["output_schema"]["required"],
            )
            == []
        )


def test_scripted_observation_cannot_complete_a_rejected_launch(scripted_reasoning):
    runner, path, launches = scripted_reasoning

    @contextmanager
    def rejected():
        raise RuntimeError("admission denied")
        yield

    assert runner.observe(path)["state"] == "unknown"
    with pytest.raises(RuntimeError, match="admission denied"):
        runner.launch(path, admission=rejected)
    observed = runner.observe(path)
    assert observed["state"] == "unknown"
    assert "receipt" not in observed
    assert launches == []


def test_scripted_observation_replays_only_recorded_completion(scripted_reasoning):
    runner, path, launches = scripted_reasoning
    admissions = []

    @contextmanager
    def admitted():
        admissions.append("enter")
        yield
        admissions.append("exit")

    first = runner.launch(path, admission=admitted)
    assert first["state"] == "finished"
    assert first["receipt"]["outcome"] == "completed"
    assert first["receipt"]["quiescent"] is True
    expected = copy.deepcopy(first)
    first["receipt"]["result"]["constraints"].append({"caller": "mutation"})
    observed = runner.observe(path)
    assert observed == expected
    observed["receipt"]["usage"]["output_tokens"] = 999
    assert runner.observe(path) == expected
    assert launches == [path]
    assert admissions == ["enter", "exit"]


@pytest.mark.parametrize(
    "claude_models",
    [
        pytest.param(None, id="cross-harness"),
        pytest.param(("claude-opus-5-5", "claude-fable-5-2"), id="opus-to-fable"),
        pytest.param(("claude-fable-5-2", "claude-opus-5-5"), id="fable-to-opus"),
    ],
)
def test_real_worker_quota_continues_same_pr_through_normal_supervisor(
    campaign, production_case, tmp_path, claude_models
):
    configured_runtime, configured, _, _ = campaign
    case = production_case
    snapshot = case.store.load(case.snapshot.id)
    fixture_roles = dict(configured.spec.roles)
    # This scenario requires an independent worker alongside the interrupted
    # unit. The single-unit handoff fixture deliberately defaults to one slot.
    fixture_roles["execution"] = dataclasses.replace(fixture_roles["execution"], max_sessions=2)
    roles = dict(snapshot.spec.roles)
    roles.update(fixture_roles)
    first, last = configured.spec.candidates("execution")
    routes = copy.deepcopy(configured.config_snapshot["reasoning_capabilities"])
    if claude_models:
        first, last = (dataclasses.replace(last, model=model) for model in claude_models)
        for candidate in (first, last):
            routes[f"{candidate.profile}:{candidate.model}"] = {
                "execution": configured_runtime.fixture_cap(candidate, True)
            }
    unavailable = dataclasses.replace(first, model=first.model + "-quota-unknown")
    routes[f"{unavailable.profile}:{unavailable.model}"] = {
        "execution": configured_runtime.fixture_cap(unavailable, True)
    }
    capabilities = tmp_path / "capabilities.json"
    capabilities.write_text(json.dumps({"schema_version": 1, "routes": routes}))
    spec = dataclasses.replace(
        snapshot.spec,
        units=(),
        roles=tuple(roles.items()),
        role_pools=(
            *snapshot.spec.role_pools,
            *(
                (name, (first, unavailable, last) if name == "writers" else pool)
                for name, pool in configured.spec.role_pools
            ),
        ),
        reasoning=dataclasses.replace(configured.spec.reasoning, capabilities=str(capabilities)),
    )
    from bmad_loop.campaign_runtime import create

    _, snapshot = create(case.repo, spec)
    # Recreate the accepted dependency added by production_case after its import;
    # create() above imports fresh source units rather than that saved graph.
    dependent = snapshot.units["source:project:dependent"]
    dependent.spec = dataclasses.replace(dependent.spec, after=("source:project:first",))
    snapshot.graph_revision += 1
    snapshot = case.store.save(snapshot, snapshot.revision)
    from bmad_loop.campaign_scheduler import CapacityObservation

    def observe_capacity(candidate, project):
        if candidate == unavailable:
            return CapacityObservation(False, known=False, reason="quota unknown for this model")
        if (
            claude_models
            and candidate == last
            and not (tmp_path / "parallel-markers/first").exists()
        ):
            return CapacityObservation(
                False, known=False, reason="awaiting model quota observation"
            )
        return CapacityObservation(True, slots=8, model_access=True)

    case.runtime.capacity_observer = observe_capacity
    case.runtime.reasoning_runner = ScriptedReasoning()
    script = tmp_path / "scripted-coding-cli.py"
    body = script.read_text()
    injection = """
    if request["unit_id"].endswith(":first"):
        workspace=Path(request["workspace"])
        partial=workspace/"src"/"first.txt"
        if not request.get("handoff_id"):
            event("execution-start",request)
            MARKERS.mkdir(exist_ok=True)
            (MARKERS/"first").write_text("started")
            partial.parent.mkdir(exist_ok=True)
            partial.write_text("preserved partial work")
            history=Path(request["execution"]).parent/"history.jsonl"
            messages=([{"type":"session_meta","payload":{"id":"quota-native"}},{"type":"event_msg","payload":{"type":"user_message","message":"Preserve partial work and all acceptance."}}] if request["candidate"]["profile"]=="codex" else [{"type":"user","sessionId":"quota-native","message":{"role":"user","content":"Preserve partial work and all acceptance."}}])
            history.write_text("\\n".join(json.dumps(row) for row in messages)+"\\n")
            event("quota",request)
            return SessionResult(status="crashed",session_id="quota-native",transcript_path=str(history),env_fault=True,env_fault_evidence="scripted quota",native_binding={"outcome":"quota","observed_model":request["candidate"]["model"],"provider_family":request["candidate"]["provider_family"]})
        packet=Path(request["handoff_packet"]["path"])
        assert hashlib.sha256(packet.read_bytes()).hexdigest()==request["handoff_packet"]["sha256"]
        assert json.loads(packet.read_text())["resolved_constraints"]
        assert partial.read_text()=="preserved partial work"
        predecessor=json.loads((Path(request["native_predecessor"]["run_dir"])/"request.json").read_text())
        assert request["candidate"]["model"]!=predecessor["candidate"]["model"]
        assert (request["workspace"],request["branch"])==(predecessor["workspace"],predecessor["branch"])
        assert request["native_predecessor"]["launch_id"]!=request["launch_id"]
        event("successor-context-delivered",request)
"""
    body = body.replace(
        "def execution(request, session):\n", "def execution(request, session):\n" + injection
    )
    body = body.replace(
        'model="gpt-6-sol" if request["kind"]=="execution" else request["candidate"]["model"]',
        'model=request["candidate"]["model"]',
    )
    body = body.replace(
        'return SessionResult(status="completed",session_id="scripted-"+request["launch_id"],result_json=artifact)',
        'history=Path(request["execution"]).parent/"stage-history.jsonl"\n    history.write_text(json.dumps({"type":"user","sessionId":"scripted-"+request["launch_id"],"isMeta":True,"message":{"role":"user","content":request["prompt"]}})+"\\n")\n    return SessionResult(status="completed",session_id="scripted-"+request["launch_id"],transcript_path=str(history),native_binding={"profile":request["candidate"]["profile"]},result_json=artifact)',
    )
    script.write_text(body)
    deadline = time.monotonic() + 300

    stop_requested = False

    def completed():
        nonlocal stop_requested
        current = case.store.load(snapshot.id)
        unit = current.units["source:project:first"]
        independent = current.units["source:project:second"]
        lineage = unit.evidence.get("session_lineage", [])
        if (
            lineage
            and any(
                row.get("outcome") == "locally_complete"
                for row in unit.evidence.get("worker_history", [])
            )
            and any(
                row.get("outcome") == "locally_complete"
                for row in independent.evidence.get("worker_history", [])
            )
            and not stop_requested
        ):
            case.store.enqueue(current.id, "stop")
            stop_requested = True
        assert time.monotonic() < deadline, json.dumps(diagnose(current), indent=2)
        return case.runtime.clock()

    supervisor = CampaignSupervisor(
        case.store, case.runtime, clock=completed, progress=lambda _: None
    )
    result = supervisor.run(snapshot.id)
    first = result.units["source:project:first"]
    assert any(
        row.get("outcome") == "locally_complete"
        for row in result.units["source:project:second"].evidence["worker_history"]
    )
    assert any(
        row.get("outcome") == "locally_complete" and row.get("head")
        for row in first.evidence["worker_history"]
    )
    assert first.evidence["developer_families"] == (
        ["anthropic"] if claude_models else ["anthropic", "openai"]
    )
    assert first.evidence["handoff"]["phase"] == "continuing"
    continuation = first.evidence["handoff"]["candidate"]
    previous = first.evidence["handoff"]["previous_route"]
    if claude_models:
        assert previous == ["claude", claude_models[0]]
        assert (continuation["profile"], continuation["model"]) == ("claude", claude_models[1])
    else:
        assert continuation["model"] in {"gpt-test", "claude-test"}
        assert continuation["profile"] != previous[0]
    assert first.evidence["session_lineage"][0]["native_session_imported"] is False
    assert (
        len(
            [
                row
                for row in case.service.pulls.values()
                if row["head"]["ref"] == first.bindings["branch"]
            ]
        )
        == 1
    )
    events = [json.loads(line) for line in case.events.read_text().splitlines()]
    assert [row["event"] for row in events if row["unit"] == first.id].count(
        "successor-context-delivered"
    ) == 1
