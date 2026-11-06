"""Production supervision, real child PIDs, scripted zero-token model responses."""

import copy
import dataclasses
import json
import time

from test_campaign_handoff_flow import campaign as campaign
from test_campaign_headless import prepared as prepared
from test_campaign_runtime import diagnose
from test_campaign_runtime import production_case as production_case

from bmad_loop.campaign_headless import HeadlessRunner, read_json
from bmad_loop.campaign_summary import SummaryJobSpec, load_input
from bmad_loop.campaign_supervisor import CampaignSupervisor
from bmad_loop.campaign_transcripts import HistorySnapshot, load_events


class ScriptedReasoning:
    """Only model execution is scripted; preparation and receipt validation are real."""

    def __init__(self):
        self.prepare = HeadlessRunner().prepare

    def launch(self, path):
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
                        "source_quote": row["text"],
                        "citations": [row["event_id"]],
                    }
                    for row in source["primary"]
                    if row["owner_input"]
                ],
            }
        else:
            assert request["purpose"] == "handoff_audit"
            events = {
                event.id: event
                for item in coordinate["history_snapshots"]
                for event in load_events(HistorySnapshot(**item))
            }
            result = {
                "handoff_id": coordinate["handoff_id"],
                "capture_digest": coordinate["capture_digest"],
                "covered_owner_ids": coordinate["owner_ids"],
                "obligation_ids": coordinate["obligation_ids"],
                "ambiguities": [],
                "constraints": [
                    {
                        "source_id": key,
                        "source_quote": events[key].record.text,
                        "interpretation": events[key].record.text,
                        "relevance": "current",
                    }
                    for key in coordinate["owner_ids"]
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

    observe = launch


def test_real_worker_quota_continues_same_pr_through_normal_supervisor(
    campaign, production_case, tmp_path
):
    _, configured, _, _ = campaign
    case = production_case
    snapshot = case.store.load(case.snapshot.id)
    fixture_roles = dict(configured.spec.roles)
    roles = dict(snapshot.spec.roles)
    roles.update(fixture_roles)
    first, last = configured.spec.candidates("execution")
    unavailable = dataclasses.replace(first, model="gpt-capacity-unavailable")
    routes = copy.deepcopy(configured.config_snapshot["reasoning_capabilities"])
    routes["codex:" + unavailable.model] = {
        "execution": {**routes["codex:" + first.model]["execution"], "model": unavailable.model}
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
    from bmad_loop.campaign_scheduler import CapacityObservation

    case.runtime.capacity_observer = lambda candidate, project: CapacityObservation(
        candidate.model != unavailable.model, slots=8, model_access=True
    )
    case.runtime.reasoning_runner = ScriptedReasoning()
    script = tmp_path / "scripted-coding-cli.py"
    body = script.read_text()
    injection = """
    if request["unit_id"].endswith(":first"):
        workspace=Path(request["workspace"])
        partial=workspace/"src"/"first.txt"
        if not request.get("handoff_id"):
            partial.parent.mkdir(exist_ok=True)
            partial.write_text("preserved partial work")
            history=Path(request["execution"]).parent/"history.jsonl"
            history.write_text(json.dumps({"type":"session_meta","payload":{"id":"quota-native"}})+"\\n"+json.dumps({"type":"event_msg","payload":{"type":"user_message","message":"Preserve partial work and all acceptance."}})+"\\n")
            event("quota",request)
            return SessionResult(status="crashed",session_id="quota-native",transcript_path=str(history),env_fault=True,env_fault_evidence="scripted quota",native_binding={"outcome":"quota","observed_model":"gpt-test","provider_family":"openai"})
        packet=Path(request["handoff_packet"]["path"])
        assert hashlib.sha256(packet.read_bytes()).hexdigest()==request["handoff_packet"]["sha256"]
        assert json.loads(packet.read_text())["resolved_constraints"]
        assert partial.read_text()=="preserved partial work"
        assert request["candidate"]["profile"]=="claude"
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
    deadline = time.monotonic() + 90

    stop_requested = False

    def completed():
        nonlocal stop_requested
        current = case.store.load(snapshot.id)
        unit = current.units["source:project:first"]
        lineage = unit.evidence.get("session_lineage", [])
        if (
            lineage
            and any(
                row.get("outcome") == "locally_complete"
                for row in unit.evidence.get("worker_history", [])
            )
            and not stop_requested
        ):
            case.store.enqueue(current.id, "stop")
            stop_requested = True
        assert time.monotonic() < deadline, diagnose(current)
        return case.runtime.clock()

    supervisor = CampaignSupervisor(
        case.store, case.runtime, clock=completed, progress=lambda _: None
    )
    result = supervisor.run(snapshot.id)
    first = result.units["source:project:first"]
    assert any(
        row.get("outcome") == "locally_complete" and row.get("head")
        for row in first.evidence["worker_history"]
    )
    assert first.evidence["developer_families"] == ["anthropic", "openai"]
    assert first.evidence["handoff"]["phase"] == "continuing"
    assert first.evidence["handoff"]["candidate"]["model"] == "claude-test"
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
