"""Renew real prepared workspaces, then resume ordinary zero-token delivery."""

import copy
import dataclasses
import json
import threading
from pathlib import Path

from conftest import git
from test_campaign_headless import prepared as prepared
from test_campaign_paradev import Host
from test_campaign_runtime import diagnose
from test_campaign_runtime import production_case as production_case
from test_campaign_runtime import run_case

from bmad_loop import (
    campaign_native_inventory,
    campaign_paradev,
    campaign_requalification,
    campaign_runtime,
)
from bmad_loop.adapters.paradev_backend import ParadevMultiplexer
from bmad_loop.campaign_action_history import actions
from bmad_loop.campaign_execution import CampaignExecution
from bmad_loop.campaign_model import ParadevSpec, ReasoningSpec, UnitState
from bmad_loop.campaign_supervisor import CampaignSupervisor


class PreparedHost(Host):
    """One synthetic host supplies both allocation and native inventory views."""

    def __init__(self, repo, root):
        super().__init__(repo, root)
        self.lock = threading.RLock()

    def rpc(self, procedure, payload=None, *, mutate=False):
        with self.lock:
            if procedure == "terminal.list":
                assert payload is not None
                assert payload["workspaceId"] in {row["id"] for row in self.workspaces}
                return {"sessions": []}
            return super().rpc(procedure, payload, mutate=mutate)

    def cli(self, args):
        with self.lock:
            if args[:2] == ["automations", "list"]:
                return []
            return super().cli(args)


def test_prepared_native_workspace_renewal_resumes_same_campaign_through_delivery(
    production_case, prepared, tmp_path, monkeypatch
):
    """A real prepare/stop receipt, not hand-made DRAFT bindings, authorizes renewal.

    Only provider installation identity, model sessions and external host/GitHub
    services are fixtures. Allocation/bootstrap, coordinator controls, capability
    validation, worker PIDs, publication and source tracking use production paths.
    """
    case = production_case
    accepted_dependencies = {key: unit.spec.after for key, unit in case.snapshot.units.items()}
    capability = prepared[4]
    manifest = tmp_path / "capabilities.json"
    manifest.write_text(
        json.dumps({"schema_version": 1, "routes": {"codex:gpt-test": capability}}),
        encoding="utf-8",
    )
    root = tmp_path / "native-worktrees"
    root.mkdir()
    host = PreparedHost(case.repo, root)
    monkeypatch.setattr(campaign_paradev, "Host", lambda: host)
    monkeypatch.setattr(campaign_native_inventory, "Host", lambda: host)
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "paradev")
    monkeypatch.setattr(
        case.runtime,
        "_native_mux",
        lambda snapshot: ParadevMultiplexer(
            host=host,
            state_dir=case.store.directory(snapshot.id) / "native-transport",
            cli_path="scripted-paradev",
        ),
    )
    spec = dataclasses.replace(
        case.snapshot.spec,
        units=(),
        paradev=ParadevSpec(
            reflect=True, project_id="project", worktree_root=str(root), tasks=False
        ),
        reasoning=ReasoningSpec(enabled=False, capabilities=str(manifest)),
    )
    _, case.snapshot = campaign_runtime.create(case.repo, spec)
    # Retain the fixture's accepted graph: its scripted dependent deliberately
    # refuses execution until First is delivered and tracking is reconciled.
    for key, unit in case.snapshot.units.items():
        unit.spec = dataclasses.replace(unit.spec, after=accepted_dependencies[key])
    case.snapshot.graph_revision += 1
    case.snapshot = case.store.save(case.snapshot, case.snapshot.revision)
    original_perform = case.runtime.perform

    def stop_after_allocation(snapshot, action):
        result = original_perform(snapshot, action)
        if action.kind == "prepare-unit" and result.state == "confirmed":
            case.store.enqueue(snapshot.id, "stop", request_id="prepared-before-native-launch")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(case.runtime, "perform", stop_after_allocation)
        stopped = run_case(case, timeout_s=120)
    assert stopped.phase == "stopped", diagnose(stopped)
    settled = CampaignSupervisor(case.store, case.runtime).reconcile(stopped.id)
    assert settled.phase == "stopped", diagnose(settled)
    assert all(row.state in {"confirmed", "rejected"} for row in settled.outbox)
    allocated = {
        key: copy.deepcopy(unit)
        for key, unit in settled.units.items()
        if unit.state == UnitState.DRAFT
    }
    assert allocated
    assert not actions(settled, kinds=("launch", "reasoning-launch"))
    assert not case.processes and not case.service.pulls
    prepared_receipts = {
        row.id: copy.deepcopy(row.receipt) for row in actions(settled, kinds=("prepare-unit",))
    }
    old_template = Path(settled.bindings["execution_template"]["path"])
    template_bytes = old_template.read_bytes()
    original_calls = copy.deepcopy(host.calls)
    heads = {
        key: git(Path(unit.bindings["workspace"]), "rev-parse", "HEAD")
        for key, unit in allocated.items()
    }
    monkeypatch.setattr(
        campaign_requalification,
        "installation",
        lambda: {"qualified": True, "commit": "a" * 40, "installation_kind": "git"},
    )
    renewed = campaign_requalification.requalify(
        case.store,
        settled.id,
        manifest,
        expected_revision=settled.revision,
        reason="Renew equivalent installed proofs before the first native session",
    )
    assert renewed.phase == "stopped" and renewed.generation == settled.generation
    assert renewed.config_revision == settled.config_revision + 1
    assert renewed.units == settled.units
    assert host.calls == original_calls and old_template.read_bytes() == template_bytes
    for key, unit in allocated.items():
        assert git(Path(unit.bindings["workspace"]), "rev-parse", "HEAD") == heads[key]
        assert unit.bindings["native"]["workspace_id"]
    completed = run_case(case, timeout_s=180)
    assert completed.phase == "delivered", diagnose(completed)
    assert all(unit.state == UnitState.DELIVERED for unit in completed.units.values())
    for key, unit in allocated.items():
        current = completed.units[key]
        for field in ("workspace", "branch", "native", "initial_source", "canonical_source"):
            assert current.bindings[field] == unit.bindings[field]
        assert current.bindings["pr"]["number"] in case.service.pulls
    for row in actions(completed, kinds=("prepare-unit",)):
        if row.id in prepared_receipts:
            assert row.receipt == prepared_receipts[row.id]
    creates = [call for call in host.calls if call[:2] == ["workspaces", "create"]]
    assert len(creates) == len(completed.units) == len(host.workspaces)
    assert len(case.service.pulls) == len(completed.units)
    assert all(row["merged"] for row in case.service.pulls.values())
    requests = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (case.store.directory(completed.id) / "workers").glob("*/request.json")
    ]
    assert requests and all(
        CampaignExecution.load(Path(row["execution"]), row["digest"]).config_revision
        == renewed.config_revision
        for row in requests
    )
