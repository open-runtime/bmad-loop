"""Explicit, zero-inference soak cycle for scripts/run_campaign_recovery_soak.py."""

import hashlib
import json
import os
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from campaign_runtime_watchdog import ProgressWatchdog
from conftest import git
from test_campaign_handoff_flow import campaign as campaign
from test_campaign_headless import prepared as prepared
from test_campaign_handoff_runtime import ScriptedReasoning, prepare_quota_handoff
from test_campaign_runtime import production_case as production_case
from test_campaign_runtime import run_case

from bmad_loop import campaign_runtime_intake as intake
from bmad_loop.campaign_github import GithubError, UncertainMutation
from bmad_loop.campaign_headless import read_json
from bmad_loop.campaign_intake_flow import INBOX
from bmad_loop.campaign_model import UnitState
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_store import CampaignStore
from bmad_loop.campaign_summary import SummaryJobSpec, load_input, quote_catalog
from bmad_loop.campaign_summary_routes import STAGES
from bmad_loop.campaign_tracking_recovery import KEY as TRACKING_RECOVERY


class SoakReasoning(ScriptedReasoning):
    """Use actual immutable summary inputs, selected facts and native result schemas."""

    def _launch(self, path):
        envelope = read_json(path)
        request = envelope["request"]
        if request["purpose"] != "handoff_reduce":
            observed = super()._launch(path)
            if request["purpose"] == "handoff_map":
                spec = SummaryJobSpec(**request["coordinates"]["summary_spec"])
                events = load_input(spec)["primary"]
                context = next((row for row in events if not row["owner_input"]), None)
                if context:
                    observed["receipt"]["result"]["facts"].append(
                        {
                            "kind": "progress",
                            "text": "Retain the historical worker context for the continued implementation.",
                            "source_quote_id": "",
                            "citations": [context["event_id"]],
                        }
                    )
            return observed
        spec = SummaryJobSpec(**request["coordinates"]["summary_spec"])
        source = load_input(spec)
        result = {
            "job_id": request["id"],
            "input_sha256": spec.input_sha256,
            "covered_ids": list(spec.primary_ids),
        }
        if "ranked_fact_ids" in request["output_schema"]["properties"]:
            result.update(
                schema_version=4,
                ranked_fact_ids=list(source["fact_catalog"]),
                required_fact_ids=[],
                excluded_fact_ids=[],
            )
        else:
            facts = [
                {
                    "kind": "owner_request",
                    "text": quote["preview"],
                    "citations": [quote["event_id"]],
                    "source_quote_id": key,
                }
                for key, quote in quote_catalog(spec).items()
            ]
            result.update(schema_version=1, gaps=[], facts=facts)
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
                "provider_family": envelope["selection"]["provider_family"],
            },
        }


def run_audited_recovery(campaign, case, tmp_path):
    routes = (("claude", "claude-opus-5-5"), ("claude", "claude-fable-5-2"))
    stage_pools = tuple(
        (stage, "auditors" if role == "handoff_audit" else "map")
        for stage, (role, _, _) in STAGES.items()
    )
    case.snapshot = prepare_quota_handoff(
        campaign, case, tmp_path, routes, summary_routes=stage_pools
    )
    case.runtime.clock = time.time
    service = SoakReasoning()
    case.runtime.reasoning_runner = service
    histories = json.loads(os.environ["RECOVERY_SOAK_HISTORIES"])
    writer = case.snapshot.config_snapshot["reasoning_capabilities"]["claude:claude-opus-5-5"][
        "execution"
    ]
    assert sum(Path(row["path"]).stat().st_size for row in histories) > writer["input_tokens"] // 2
    script = tmp_path / "scripted-coding-cli.py"
    source = script.read_text()
    before = "            history.write_text("
    assert source.count(before) == 1
    replay = """
            for archived in __HISTORIES__:
                raw=Path(archived["path"]).read_bytes()
                assert hashlib.sha256(raw).hexdigest()==archived["sha256"]
                messages.append({"type":"user","sessionId":"quota-native","isMeta":True,"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":archived["sha256"],"content":raw.decode("utf-8")}]}})
"""
    original = 'partial.write_text("preserved partial work")'
    retained = 'assert partial.read_text()=="preserved partial work"'
    assert source.count(original) == source.count(retained) == 1
    source = source.replace(before, replay.replace("__HISTORIES__", repr(histories)) + before)
    source = source.replace(
        original,
        """partial.write_text("preserved partial work")
            relative=Path(request["source"]["path"])/"stories"/"first-story.md"
            spec=workspace/relative
            spec.write_bytes(spec.read_bytes()+b"\\n<!-- Staged implementation note. -->\\n")
            assert verify.git_bytes(workspace,"add",str(relative)).returncode==0
            spec.write_bytes(spec.read_bytes()+b"<!-- Unstaged implementation note. -->\\n")""",
    )
    source = source.replace(
        retained,
        retained
        + """
        relative=Path(request["source"]["path"])/"stories"/"first-story.md"
        index=verify.git_bytes(workspace,"show",":"+str(relative)).stdout
        assert index.endswith(b"<!-- Staged implementation note. -->\\n")
        assert (workspace/relative).read_bytes()==index+b"<!-- Unstaged implementation note. -->\\n"
        assert verify.git_bytes(workspace,"ls-files","--others","--exclude-standard","--","src/first.txt").stdout.strip()==b"src/first.txt"
        assert (request["workspace"],request["branch"],request["pr_number"]) == (
            request["native_predecessor"]["workspace"],request["native_predecessor"]["branch"],
            json.loads((Path(request["native_predecessor"]["run_dir"])/"request.json").read_text())["pr_number"])""",
    )
    script.write_text(source)
    save = case.store.save

    class CrashAtDurableHandoff(Exception):
        pass

    def crash_at_quota(*args, **kwargs):
        saved = save(*args, **kwargs)
        first = saved.units["source:project:first"]
        handoff = first.evidence.get("handoff") or {}
        if handoff.get("phase") == "drain" and first.claim.quiescent:
            assert handoff["receipt"]["interruption"]["kind"] == "quota"
            case.store.enqueue(saved.id, "stop", reason="Scripted stop during a quota handoff")
            raise CrashAtDurableHandoff
        return saved

    case.store.save = crash_at_quota
    try:
        with pytest.raises(CrashAtDurableHandoff):
            run_case(case)
    finally:
        case.store.save = save
    predecessor = case.store.load(case.snapshot.id).units["source:project:first"]
    old = case.runtime
    case.store = CampaignStore(case.store.root)
    case.runtime = CampaignRuntime(
        case.repo,
        case.store,
        gateway=old.gateway(case.snapshot),
        clock=time.time,
        capacity_observer=old.capacity_observer,
    )
    case.runtime.reasoning_runner = service
    assert run_case(case).phase == "stopped"
    settled = run_case(case)
    first = settled.units[predecessor.id]
    assert first.state == UnitState.DELIVERED
    assert (
        first.bindings["workspace"],
        first.bindings["branch"],
        first.bindings["pr"]["number"],
    ) == (
        predecessor.bindings["workspace"],
        predecessor.bindings["branch"],
        predecessor.bindings["pr"]["number"],
    )
    assert first.evidence["developer_families"] == ["anthropic"]
    assert first.evidence["handoff"]["candidate"]["model"] == routes[1][1]
    assert tuple(first.evidence["handoff"]["previous_route"]) == routes[0]
    capture = first.evidence["handoff"]["capture"]
    assert not capture["gaps"] and capture["obligations"]
    archived_ids = {row["sha256"] for row in histories}
    recovered = set()
    for item in capture["sources"]:
        if item["session_id"] != "quota-native":
            continue
        data = Path(item["raw_path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == item["raw_sha256"]
        assert len(data) == item["complete_bytes"] == item["captured_bytes"]
        for line in data.splitlines():
            content = json.loads(line).get("message", {}).get("content")
            if isinstance(content, list):
                for entry in content:
                    if entry.get("tool_use_id") in archived_ids:
                        assert (
                            hashlib.sha256(entry["content"].encode()).hexdigest()
                            == entry["tool_use_id"]
                        )
                        recovered.add(entry["tool_use_id"])
    assert recovered == archived_ids
    jobs = Counter(read_json(path)["request"]["purpose"] for path in service.completed)
    assert jobs["handoff_map"] and jobs["handoff_reduce"] and jobs["handoff_audit"]
    events = [json.loads(line) for line in case.events.read_text().splitlines()]
    assert (
        sum(row["event"] == "quota" for row in events)
        == sum(row["event"] == "successor-context-delivered" for row in events)
        == 1
    )
    return settled, dict(jobs)


def install_scripted_disposition(script, marker):
    """Answer the real triage contract for the informational edit in this workload."""
    body = script.read_text()
    supported = '"decomposition","planning_review"}'
    insertion = '    if name=="publication_review":'
    assert body.count(supported) == body.count(insertion) == 1
    answer = """
    if name == "triage":
        contract = context["triage_result_contract"]["intake_coordinates"]
        item, = context["intake"]["observations"]
        assert item["payload"]["body"].endswith(__MARKER__)
        target, = [row for row in context["existing_units"]
                   if "Source: " + row["id"] + "\\n" in item["payload"]["body"]]
        artifact.update(intake_coordinates=contract, units=[], proposal={
            "schema_version": 1, "disposition": "duplicate", "input_versions": contract["input_versions"],
            "unit_id": target["id"], "rationale": "The dated maintenance note adds no new work to the existing acceptance.",
            "acceptance_criteria": target["acceptance"], "dependencies": target["after"],
            "related_issue": item["number"], "related_pr": None, "within_scope": True,
            "graph_revision": request["coordinates"]["graph_revision"],
        })
"""
    script.write_text(
        body.replace(supported, '"decomposition","planning_review","triage"}').replace(
            insertion, answer.replace("__MARKER__", repr(marker)) + insertion
        )
    )


def test_recovery_soak_cycle(campaign, production_case, tmp_path, monkeypatch):
    result_path = os.environ.get("RECOVERY_SOAK_REPORT")
    if not result_path:
        pytest.skip("invoke scripts/run_campaign_recovery_soak.py with an evidence directory")
    case = production_case
    service = case.service
    service.drop_merge_reply = True
    service.move_before_first_merge = True
    started = time.monotonic()
    timestamps = {}
    api_calls = Counter()
    merge_submissions = Counter()
    samples = []
    auth = {}
    lost_ack = {}
    comments = {}
    marker = "\n\nThe repository owner confirmed the migration date."
    install_scripted_disposition(case.events.parent / "scripted-coding-cli.py", marker)

    def mark(name):
        timestamps.setdefault(name, time.monotonic() - started)

    request = service.request

    def fault_transport(method, path, payload=None):
        # The same server lock serializes the external edit with the simulated
        # GitHub write. Its acknowledged response remains a distinct preimage.
        with service.lock:
            api_calls[method] += 1
            if method == "PUT" and path.endswith("/merge-async"):
                merge_submissions[path] += 1
            if method == "GET" and path == auth.get("path"):
                if "until" not in auth and threading.get_ident() == auth["writer"]:
                    auth["until"] = time.monotonic() + 3
                if time.monotonic() < auth.get("until", 0):
                    mark("authentication_refused")
                    raise GithubError(
                        "Scripted authentication expired", status=401, request_id="soak-auth"
                    )
            try:
                response = request(method, path, payload)
            except UncertainMutation:
                if method == "PUT" and path.endswith("/merge-async"):
                    lost_ack["pull"] = int(path.split("/")[-2])
                    mark("merge_acknowledgement_lost")
                raise
            parsed = urlsplit(path)
            if parsed.path.endswith("/comments"):
                if method == "POST":
                    stamp = datetime.now(timezone.utc).isoformat()
                    row = {
                        **response.data,
                        "user": {"id": 21, "login": "campaign-bot"},
                        "author_association": "MEMBER",
                        "created_at": stamp,
                        "updated_at": stamp,
                    }
                    comments.setdefault(parsed.path, []).append(row)
                    return service._response(row, status=response.status)
                if method == "GET":
                    query = parse_qs(parsed.query)
                    page = int(query.get("page", ["1"])[0])
                    count = int(query.get("per_page", ["30"])[0])
                    return service._response(
                        comments.get(parsed.path, [])[(page - 1) * count : page * count]
                    )
            if (
                lost_ack
                and path == "graphql"
                and "mergeCommit" in payload["query"]
                and payload["variables"].get("number") == lost_ack["pull"]
            ):
                mark("merged_commit_read_back")
            if (
                not auth
                and method == "PATCH"
                and "/issues/" in path
                and payload.get("state") == "closed"
                and payload.get("state_reason") == "completed"
            ):
                number = int(path.split("/")[-1])
                service.issues[number]["body"] += marker
                service._save()
                auth.update(path=path, number=number, writer=threading.get_ident())
                mark("acknowledged_close_edited")
            return response

    monkeypatch.setattr(service, "request", fault_transport)
    base_milestones = ProgressWatchdog._milestones

    def soak_milestones(watchdog, snapshot):
        milestones = base_milestones(watchdog, snapshot)
        first = snapshot.units.get("source:project:first")
        handoff = first.evidence.get("handoff") if first else None
        if handoff:
            milestones.add((first.id, handoff["id"], handoff["phase"]))
            # Model polling does not reset the stall clock. A newly accepted
            # immutable result for this handoff is productive work.
            for row in snapshot.events[-16:]:
                if row.get("kind") == "summary-result-applied" and row.get("job_id") in handoff.get(
                    "all_jobs", []
                ):
                    milestones.add((first.id, "summary-applied", row["job_id"]))
        # One persisted 401 and its one actual expiry are distinct milestones.
        # The ordinary idle watchdog and its absolute ceiling remain in force.
        if "deadline" in auth:
            milestones.add(("soak", "tracking-authentication", "wait-persisted"))
            if time.time() >= auth["deadline"]:
                milestones.add(("soak", "tracking-authentication", "retry-due"))
        return milestones

    monkeypatch.setattr(ProgressWatchdog, "_milestones", soak_milestones)
    enqueue = CampaignStore.enqueue
    save = CampaignStore.save
    reconcile = CampaignRuntime._perform_tracking_reconcile

    def observe_enqueue(store, campaign_id, kind, **kwargs):
        if kind == "stop":
            mark("stop_enqueued")
        return enqueue(store, campaign_id, kind, **kwargs)

    def observe_save(store, snapshot, *args, **kwargs):
        saved = save(store, snapshot, *args, **kwargs)
        if saved.id != case.snapshot.id:
            return saved
        first = saved.units.get("source:project:first")
        if (
            first
            and first.hold
            and (
                first.hold.kind == "quota"
                or ((first.evidence.get("handoff") or {}).get("receipt") or {})
                .get("interruption", {})
                .get("kind")
                == "quota"
            )
        ):
            mark("quota_held")
        if saved.phase == "stopped" and "stop_enqueued" in timestamps:
            mark("stop_settled")
        if saved.phase == "active" and "stop_settled" in timestamps:
            mark("resumed_after_stop")
        if "authentication_refused" in timestamps and "deadline" not in auth:
            for unit in saved.units.values():
                for predecessor, debt in unit.evidence.get(TRACKING_RECOVERY, {}).items():
                    fault = (debt.get("acknowledged") or {}).get("fault") or {}
                    if (
                        fault.get("request_id") == "soak-auth"
                        and fault.get("category") == "authentication"
                    ):
                        auth.update(action=predecessor, deadline=debt["next_retry_at"])
                        mark("authentication_wait_persisted")
        elapsed = time.monotonic() - started
        if not samples or elapsed - samples[-1]["seconds"] >= 5 or saved.phase == "delivered":
            root = store.directory(saved.id)
            sizes = {}
            for name in ("campaign.sqlite", "campaign.sqlite-wal", "events.jsonl"):
                try:
                    sizes[name] = (root / name).stat().st_size
                except FileNotFoundError:
                    sizes[name] = 0
            samples.append(
                {
                    "seconds": elapsed,
                    "generation": saved.generation,
                    "phase": saved.phase,
                    "outbox": len(saved.outbox),
                    "reasoning_jobs": len(saved.jobs),
                    "authority_bytes": sizes,
                    "active_claims": dict(
                        Counter(
                            unit.claim.resource
                            for unit in saved.units.values()
                            if unit.claim and not unit.claim.quiescent
                        )
                    ),
                    "live_scripted_workers": sum(
                        p.poll() is None for p in list(case.processes.values())
                    ),
                    "api_requests": sum(api_calls.values()),
                }
            )
            if os.name == "posix":
                import resource

                rss_unit = 1 if sys.platform == "darwin" else 1024
                samples[-1]["pytest_peak_rss_bytes"] = (
                    resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * rss_unit
                )
                samples[-1]["finished_child_peak_rss_bytes"] = (
                    resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * rss_unit
                )
            with (
                Path(result_path)
                .with_suffix(".samples.jsonl")
                .open("a", encoding="utf-8") as stream
            ):
                stream.write(json.dumps(samples[-1]) + "\n")
        return saved

    def observe_reconcile(runtime, snapshot, action):
        if "action" in auth and action.payload.get("predecessor") == auth["action"]:
            auth.setdefault("reconciled_at", time.time())
            mark("authentication_reconciled")
        return reconcile(runtime, snapshot, action)

    monkeypatch.setattr(CampaignStore, "enqueue", observe_enqueue)
    monkeypatch.setattr(CampaignStore, "save", observe_save)
    monkeypatch.setattr(CampaignRuntime, "_perform_tracking_reconcile", observe_reconcile)
    evidence = {"status": "incomplete", "timestamps_s": timestamps, "samples": samples}
    try:
        # The maintained scenario interrupts a supervisor immediately after its
        # durable successor reservation and restores a fresh supervisor/store.
        # Its own assertions inspect staged and unstaged bytes, PR, predecessor,
        # consumed permit, generation and exactly one native successor event.
        settled, reasoning_jobs = run_audited_recovery(campaign, case, tmp_path)
        required = {
            "quota_held",
            "stop_enqueued",
            "stop_settled",
            "resumed_after_stop",
            "merge_acknowledgement_lost",
            "merged_commit_read_back",
            "acknowledged_close_edited",
            "authentication_refused",
            "authentication_wait_persisted",
            "authentication_reconciled",
        }
        assert not (required - timestamps.keys()), sorted(required - timestamps.keys())
        assert timestamps["merged_commit_read_back"] >= timestamps["merge_acknowledgement_lost"]
        assert auth["reconciled_at"] >= auth["deadline"]
        assert service.issues[auth["number"]]["body"].endswith(marker)
        close_writes = Counter(
            path
            for method, path, payload in service.calls
            if method == "PATCH" and "/issues/" in path and payload.get("state") == "closed"
        )
        assert close_writes[auth["path"]] == 1
        assert set(close_writes.values()) == {1}
        assert service.moved_main
        # A nonwork disposition can own a closed, unmerged administrative
        # PR in older candidates. Report this cost separately from delivery.
        stories = [unit for unit in settled.units.values() if not intake.control(unit)]
        controls = [unit for unit in settled.units.values() if intake.control(unit)]
        assert len(stories) == 3 and all(unit.state == UnitState.DELIVERED for unit in stories)
        assert len(controls) <= 1 and all(intake.optional_canceled(unit) for unit in controls)
        assert intake.settled(settled)
        assert any(unit.bindings["issue"]["number"] == auth["number"] for unit in stories)
        assert any(
            row["observation"]["number"] == auth["number"]
            and row["observation"]["payload"]["body"].endswith(marker)
            and (row.get("disposition") or {}).get("disposition") == "duplicate"
            for row in settled.bindings[INBOX]["records"].values()
        )
        story_prs = {unit.bindings["pr"]["number"] for unit in stories}
        control_prs = {
            unit.bindings["pr"]["number"] for unit in controls if unit.bindings.get("pr")
        }
        assert story_prs.isdisjoint(control_prs)
        assert set(service.pulls) == story_prs | control_prs
        assert all(
            service.pulls[number]["state"] == "closed" and not service.pulls[number]["merged"]
            for number in control_prs
        )
        assert len(merge_submissions) == len(story_prs) == 3
        assert set(merge_submissions.values()) == {1}
        assert all(service.pulls[number]["merged"] for number in story_prs)
        assert all(
            git(case.remote, "merge-base", pr["merge_commit_sha"], "refs/heads/main")
            == pr["merge_commit_sha"]
            for pr in (service.pulls[number] for number in story_prs)
        )
        assert settled.phase == "delivered"
        assert samples[-1]["phase"] == "delivered"
        evidence.update(
            status="passed",
            reasoning_jobs=reasoning_jobs,
            campaign_id=settled.id,
            generation=settled.generation,
            delivered_units=len(stories),
            extra_intake_workspaces=sum(bool(unit.bindings.get("workspace")) for unit in controls),
            extra_intake_prs=sorted(control_prs),
            issue_close_submissions=dict(close_writes),
            stop_latency_s=timestamps["stop_settled"] - timestamps["stop_enqueued"],
            auth_retry_deadline=auth["deadline"],
            auth_retry_started=auth["reconciled_at"],
            configured_max_sessions=settled.spec.resources.max_sessions,
            configured_verification_slots=settled.spec.resources.verify_slots,
        )
    finally:
        evidence.update(
            elapsed_s=time.monotonic() - started,
            api_methods=dict(api_calls),
            merge_submissions=dict(merge_submissions),
        )
        with Path(result_path).open("x", encoding="utf-8") as stream:
            json.dump(evidence, stream, indent=2)
            stream.write("\n")
