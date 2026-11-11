"""Zero-token startup through real discovery, ownership and worker admission.

The existing production fixture supplies real Git, worktrees, validated child
receipts and scripted coding sessions. Only GitHub/Paradev services and provider
capacity are fake. This test drives normal proposals and production effects; it
stops at coding startup, without qualifying advisory transport or publication.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from campaign_runtime_watchdog import ProgressWatchdog
from conftest import git
from test_campaign_headless import prepared as prepared
from test_campaign_paradev import Host as WorkspaceHost
from test_campaign_runtime import diagnose
from test_campaign_runtime import production_case as production_case

from bmad_loop import campaign_native_inventory, campaign_paradev, campaign_runtime
from bmad_loop.adapters import paradev_host
from bmad_loop.campaign_github import ApiResponse
from bmad_loop.campaign_model import ParadevSpec, ReasoningSpec, RoleSpec
from bmad_loop.campaign_store import CampaignStore
from bmad_loop.campaign_supervisor import CampaignSupervisor


def test_startup_recovers_complete_discovery_before_allocating_or_planning(
    production_case, prepared, tmp_path, monkeypatch
):
    """Ablation: restoring the old aggregate byte allowance prevents complete admission."""
    case = production_case
    runtime = case.runtime
    _, _, _, candidate, capability = prepared
    baseline = case.store.load(case.snapshot.id)
    root = tmp_path / "native-workspaces"
    root.mkdir()

    class Host(WorkspaceHost):
        def __init__(self, *args):
            super().__init__(*args)
            self.lock = threading.RLock()
            self.errors = []
            self.daemon_reads = 0

        def rpc(self, procedure, payload=None, mutate=False):
            with self.lock:
                if procedure == "terminal.list":
                    return {"sessions": []}
                if procedure == "terminal.daemon.listSessions":
                    self.daemon_reads += 1
                return super().rpc(procedure, payload, mutate=mutate)

        def cli(self, args):
            # The base host fixture is serial. A service must assign unique
            # workspace IDs/paths atomically for concurrent allocation calls.
            with self.lock:
                if args[:2] == ["automations", "list"]:
                    return []
                try:
                    return super().cli(args)
                except subprocess.CalledProcessError as exc:
                    self.errors.append({"argv": exc.cmd, "stderr": exc.stderr})
                    raise

    host = Host(case.repo, root)

    # A developer's installed host must not mask an unpatched native client.
    # Isolate this client's discovery only, without changing process HOME.
    class HostPath(type(tmp_path)):
        @classmethod
        def home(cls):
            return cls(tmp_path / "absent-native-host")

    monkeypatch.setattr(paradev_host, "Path", HostPath)
    with pytest.raises(paradev_host.HostError, match="HOST_MANIFEST_NOT_UNIQUE"):
        paradev_host.Host()
    monkeypatch.setattr(campaign_paradev, "Host", lambda: host)
    monkeypatch.setattr(campaign_native_inventory, "Host", lambda: host)
    monkeypatch.setattr(paradev_host, "Host", lambda: host)
    # Configure a new campaign through the real create path; the existing
    # production fixture supplies the source checkout and child/service seams.
    manifest = tmp_path / "qualified-fixture-capabilities.json"
    manifest.write_text(
        json.dumps(
            {"schema_version": 1, "routes": {candidate.profile + ":" + candidate.model: capability}}
        )
    )
    role = RoleSpec(profile=candidate.profile, model=candidate.model, effort=candidate.effort)
    spec = dataclasses.replace(
        baseline.spec,
        units=(),
        sources=(dataclasses.replace(baseline.spec.sources[0], units=("first", "second")),),
        ownership=tuple(
            (key, scopes)
            for key, scopes in baseline.spec.ownership
            if not key.endswith(":dependent")
        ),
        paradev=ParadevSpec(reflect=True, project_id="project", worktree_root=str(root)),
        reasoning=ReasoningSpec(
            enabled=True, capabilities=str(manifest), purposes=("graph_review", "priority")
        ),
        roles=(*baseline.spec.roles, ("graph_review", role), ("reasoning", role)),
    )
    store = CampaignStore(tmp_path / "startup-state")
    monkeypatch.setattr(campaign_runtime, "store_for", lambda _: store)
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "paradev")
    store, snapshot = campaign_runtime.create(case.repo, spec)
    runtime.store = store
    # Exercise the actual lazy backend client before any long supervisor wait.
    assert runtime._native_mux(snapshot)._host() is host
    # Ordinary persistence exceeds the old native snapshot byte limit. The
    # migration inventory must read bounded ownership, not recursively load it.
    snapshot.bindings["historical-unrelated-observation"] = "x" * (4 * 1024 * 1024 + 1)
    snapshot = store.save(snapshot, snapshot.revision)

    base = git(case.repo, "rev-parse", "HEAD")
    git(case.repo, "switch", "-c", "preserved-unfinished")
    (case.repo / "src").mkdir(exist_ok=True)
    (case.repo / "src/first.txt").write_text("partial prototype, not accepted behavior\n")
    (case.repo / "unrelated-history.txt").write_text("unrelated history\n" * 70000)
    git(case.repo, "add", "src/first.txt", "unrelated-history.txt")
    git(case.repo, "commit", "-qm", "Preserved partial prototype and unrelated history")
    git(case.repo, "push", "origin", "HEAD:refs/heads/preserved-unfinished")
    git(case.repo, "switch", "main")
    subprocess.run(
        ["git", "-C", str(case.remote), "update-ref", "--stdin"],
        input="".join(f"update refs/heads/checkpoint/{i:04d} {base}\n" for i in range(140)),
        text=True,
        capture_output=True,
        check=True,
    )

    original_request = case.service.request
    recovery = {"available": False}
    pages = []
    unrelated_body = "An audit #22626 is not a selected issue.\n" + "retained context\n" * 3500
    unrelated_issues = [
        {
            "id": 9002 + index,
            "number": 9002 + index,
            "title": f"Unrelated second page issue {index}",
            "body": unrelated_body,
            "state": "open",
        }
        for index in range(80)
    ]
    assert len(json.dumps(unrelated_issues).encode()) > 4_000_000

    def request(method, path, payload=None):
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        if method == "GET" and parsed.path == "repos/a/b":
            return ApiResponse(200, {"id": 123, "full_name": "a/b"}, {})
        if method == "GET" and parsed.path == "repos/a/b/issues" and query.get("state") == ["open"]:
            pages.append(dict(query))
            if query.get("page") == ["2"]:
                if not recovery["available"]:
                    return ApiResponse(503, {"message": "temporary page outage"}, {})
                return ApiResponse(
                    200,
                    unrelated_issues,
                    {},
                )
            result = original_request(method, path, payload)
            next_query = {key: value[0] for key, value in query.items()}
            next_query["page"] = "2"
            return ApiResponse(
                200,
                result.data,
                {
                    "link": "<https://api.github.com/repositories/123/issues?"
                    + urlencode(next_query)
                    + '>; rel="next"'
                },
            )
        return original_request(method, path, payload)

    monkeypatch.setattr(case.service, "request", request)
    offset = [0.0]
    runtime.clock = lambda: time.time() + offset[0]
    supervisor = CampaignSupervisor(store, runtime, clock=runtime.clock, progress=lambda _: None)
    watchdog = ProgressWatchdog(snapshot, now=time.monotonic())

    def settle(current, proposals, executor):
        current = supervisor._queue(current, tuple(proposals))
        current = supervisor._dispatch_ready(current, executor)
        while supervisor._active:
            assert not watchdog.check(current, now=time.monotonic()), diagnose(current)
            current = supervisor._harvest(current)
            assert not host.errors, host.errors
            if supervisor._active:
                threading.Event().wait(0.01)
        return current

    with store.coordinator(snapshot.common_dir, snapshot.spec.target, campaign_id=snapshot.id):
        with supervisor._executors() as executor:
            assert not runtime.observe_workers(snapshot)
            snapshot = settle(snapshot, runtime.proposals(snapshot), executor)
            inventory = snapshot.bindings["inventory"]
            assert not inventory["complete"]
            assert any("503" in fault for fault in inventory["unavailable"])
            assert not host.workspaces and not case.processes and not snapshot.jobs
            assert runtime.proposals(snapshot) == ()
            assert not [p for p in runtime.observe_workers(snapshot) if p.kind == "semantic-plan"]

            recovery["available"] = True
            offset[0] = 16.0  # normal retry threshold, not a fabricated complete flag
            snapshot = settle(snapshot, runtime.proposals(snapshot), executor)
            inventory = snapshot.bindings["inventory"]
            assert inventory["complete"], (inventory["unavailable"], inventory["truncated"])
            assert any(row["number"] == 9002 for row in inventory["issues"])
            retained = [row for row in inventory["issues"] if 9002 <= row["number"] < 9082]
            assert len(retained) == 80
            assert all(row["body"] == unrelated_body for row in retained)
            branch = next(
                row
                for row in inventory["remote_refs"]
                if row["ref"] == "refs/heads/preserved-unfinished"
            )
            assert "partial prototype" in branch["branch_evidence"]["diff"]
            assert "unrelated history" not in branch["branch_evidence"]["diff"]
            assert len(inventory["remote_refs"]) > 128
            assert not snapshot.bindings["external_ownership"]["unavailable"]
            assert not snapshot.bindings["external_ownership"]["truncated"]
            owned = [
                row
                for row in snapshot.bindings["external_ownership"]["items"]
                if row["kind"] == "native-unit"
            ]
            assert {row["details"]["campaign_id"] for row in owned} == {snapshot.id}
            assert not host.workspaces and not snapshot.jobs
            assert not [p for p in runtime.observe_workers(snapshot) if p.kind == "semantic-plan"]

            # Native reflection and headless advisory transport have separate
            # qualifications. Here advance actual semantic-plan creation and
            # coding-worker observation, leaving optional advice pending while
            # normal unit proposals allocate and launch the scripted workers.
            started = set()
            semantic_created = False
            daemon_reads_before_workers = host.daemon_reads
            while started != {"source:project:first", "source:project:second"}:
                assert not watchdog.check(snapshot, now=time.monotonic()), diagnose(snapshot)
                observations = tuple(
                    p
                    for p in runtime.observe_workers(snapshot)
                    if p.kind == "observe-worker"
                    or (p.kind == "semantic-plan" and not semantic_created)
                )
                if any(p.kind == "semantic-plan" for p in observations):
                    assert any(unit.bindings.get("native") for unit in snapshot.units.values())
                snapshot = settle(snapshot, observations, executor)
                semantic_created = semantic_created or bool(snapshot.jobs)
                snapshot = settle(snapshot, runtime.proposals(snapshot), executor)
                if case.events.exists():
                    started = {
                        row["unit"]
                        for row in map(json.loads, case.events.read_text().splitlines())
                        if row["event"] == "execution-start"
                    }
                threading.Event().wait(0.01)
            assert snapshot.bindings["capacity"]["observations"]
            assert host.daemon_reads > daemon_reads_before_workers
            assert len(host.workspaces) == 2
            assert semantic_created and snapshot.jobs
            assert all(not row.get("claim") for row in snapshot.jobs.values())
            assert all(
                unit.bindings.get("native")
                for unit in snapshot.units.values()
                if unit.id in started
            )
            assert all(
                case.service.pulls[unit.bindings["pr"]["number"]]["draft"]
                for unit in snapshot.units.values()
                if unit.id in started
            )
            assert any(query.get("page") == ["2"] for query in pages)
