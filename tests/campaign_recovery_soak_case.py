"""Explicit, zero-inference soak cycle for scripts/run_campaign_recovery_soak.py."""

import json
import os
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import pytest
from conftest import git
from test_campaign_handoff_flow import campaign as campaign
from test_campaign_headless import prepared as prepared
from test_campaign_rearm_runtime import exercise_quota_restart
from test_campaign_runtime import production_case as production_case

from bmad_loop import campaign_runtime_intake as intake
from bmad_loop.campaign_github import GithubError, UncertainMutation
from bmad_loop.campaign_model import UnitState
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_store import CampaignStore
from bmad_loop.campaign_tracking_recovery import KEY as TRACKING_RECOVERY


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
        if first and first.hold and first.hold.kind == "quota":
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
        settled = exercise_quota_restart(campaign, case, tmp_path, "unlaunched_stop")
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
