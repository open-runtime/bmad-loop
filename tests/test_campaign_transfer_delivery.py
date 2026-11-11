"""Archived startup transfer reaches merged delivery through the normal supervisor."""

import copy
import dataclasses
import json
import threading
from pathlib import Path

from conftest import git
from test_campaign_runtime import diagnose
from test_campaign_runtime import production_case as production_case
from test_campaign_runtime import run_case
from test_campaign_transfer import Host as ArchivedHost

from bmad_loop import campaign_adoption as adoption
from bmad_loop import campaign_native_inventory, campaign_paradev, campaign_runtime, verify
from bmad_loop.adapters import paradev_host
from bmad_loop.campaign_model import ParadevSpec, UnitState
from bmad_loop.campaign_store import CampaignStore, add_action
from bmad_loop.campaign_transfer import TransferRequest, read_release, release


def test_archived_startup_becomes_new_workspace_same_pr_and_delivered_story(
    production_case, tmp_path, monkeypatch
):
    case = production_case
    root = tmp_path / "native-workspaces"
    root.mkdir()

    class Host(ArchivedHost):
        def __init__(self):
            super().__init__(case.repo, root)
            self.lock = threading.RLock()

        def rpc(self, procedure, payload=None, mutate=False):
            with self.lock:
                return super().rpc(procedure, payload, mutate=mutate)

        def cli(self, args):
            with self.lock:
                if args[:2] != ["workspaces", "create"]:
                    return super().cli(args)
                branch = args[args.index("--branch") + 1]
                if branch not in git(case.repo, "branch", "--format=%(refname:short)").splitlines():
                    return super().cli(args)
                # The actual native service checks out a preserved local branch.
                self.calls.append(args)
                checkout = root / ("unit-" + str(len(self.workspaces)))
                git(case.repo, "worktree", "add", str(checkout), branch)
                row = {
                    "id": "workspace-" + str(len(self.workspaces)),
                    "projectId": "project",
                    "worktreePath": str(checkout),
                    "branch": branch,
                    "tags": args[args.index("--tag") + 1],
                    "name": args[args.index("--name") + 1],
                }
                self.workspaces.append(row)
                return copy.deepcopy(row)

    host = Host()
    monkeypatch.setattr(campaign_paradev, "Host", lambda: host)
    monkeypatch.setattr(campaign_native_inventory, "Host", lambda: host)
    monkeypatch.setattr(paradev_host, "Host", lambda: host)
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "paradev")
    store = CampaignStore(tmp_path / "transfer-delivery-state")
    monkeypatch.setattr(campaign_runtime, "store_for", lambda _: store)
    unit_id = "source:project:first"
    spec = dataclasses.replace(
        case.snapshot.spec,
        units=(),
        sources=(dataclasses.replace(case.snapshot.spec.sources[0], units=("first",)),),
        ownership=tuple(row for row in case.snapshot.spec.ownership if row[0] == unit_id),
        paradev=ParadevSpec(reflect=True, project_id="project", worktree_root=str(root)),
    )
    store, old = campaign_runtime.create(case.repo, spec, prepare_only=True)
    runtime = case.runtime
    runtime.store = store
    old.units[unit_id].state = UnitState.PLANNING
    action = add_action(old, "prepare-unit", unit_id, {})
    old = store.save(old, old.revision)
    action = next(row for row in old.outbox if row.id == action.id)
    observed = runtime.perform(old, action)
    assert observed.state == "confirmed", observed.reason
    runtime.apply(old, action, observed)
    action.state, action.receipt = "confirmed", observed.data
    old = store.save(old, old.revision)
    binding = old.units[unit_id].bindings
    workspace = Path(binding["workspace"])
    assert git(workspace, "status", "--porcelain") == ""
    git(workspace, "push", "origin", binding["branch"])
    pr = case.service.request(
        "POST",
        "repos/a/b/pulls",
        {
            "head": binding["branch"],
            "base": "main",
            "title": "Preserved startup specification",
            "body": "Keep the original discussion and specification commit.",
            "draft": True,
        },
    ).data
    case.service.pulls[pr["number"]]["head"]["repo"] = {"full_name": "a/b"}
    case.service._save()
    git(case.repo, "worktree", "remove", str(workspace))
    host.workspaces[0].update(
        archivedAt="archive-v1",
        archiveReason="deleted",
        worktreeExists=False,
        headSha=binding["head"],
    )
    tombstone = copy.deepcopy(host.workspaces[0])
    _, successor = campaign_runtime.create(case.repo, spec, prepare_only=True)
    released = release(
        store,
        TransferRequest(
            old.id,
            unit_id,
            successor.id,
            unit_id,
            pr["number"],
            old.revision,
            successor.revision,
            "Continue the archived startup specification on its preserved PR",
        ),
        project=case.repo,
        gateway=runtime.gateway(successor),
    )
    retirement = store.load(old.id)

    def resolver(name, **kwargs):
        return adoption.load_provider(name, store=store, **kwargs)

    runtime.adoption_resolver = resolver
    proposed = adoption.propose(
        case.repo,
        successor,
        unit_id,
        pr["number"],
        "campaign",
        gateway=runtime.gateway(successor),
        resolver=resolver,
    )
    assert proposed["ready_for_owner_authorization"], proposed["problems"]
    manifest = tmp_path / "transfer-adoption.json"
    manifest.write_text(json.dumps({**proposed["manifest"], "authorized_by": "owner-test"}))
    payload = adoption.prepare_request(
        case.repo,
        successor,
        unit_id,
        manifest,
        "campaign",
        gateway=runtime.gateway(successor),
        resolver=resolver,
    )
    store.enqueue(successor.id, "adopt", unit=unit_id, adoption=payload)
    script = case.events.parent / "scripted-coding-cli.py"
    text = script.read_text().replace('if suffix in {"first","second"}:', "if False:")
    needle = '    return SessionResult(status="completed",session_id="scripted-"+request["launch_id"],result_json=artifact)'
    assert text.count(needle) == 1
    text = text.replace(
        needle,
        f"""    if name=="admission_review" and request["unit_id"]=={unit_id!r}:
        pull=next(row for row in context["inventory"]["pulls"] if row["number"]=={pr["number"]})
        artifact["overlap"].update(disposition="continue_existing_work",adopted_pr=pull["number"],evidence_ids=["pr:"+str(pull["number"])+"@"+pull["head"]["sha"]],reason="Continue the exact owner-authorized startup transfer and retained source obligations")
""" + needle,
    )
    script.write_text(text)
    case.store, case.snapshot = store, successor
    delivered = run_case(case)
    assert delivered.phase == "delivered", diagnose(delivered)
    unit = delivered.units[unit_id]
    assert unit.state == UnitState.DELIVERED
    assert unit.bindings["pr"]["number"] == pr["number"]
    assert len(case.service.pulls) == 1
    assert unit.bindings["native"]["workspace_id"] != tombstone["id"]
    assert unit.bindings["branch"] == binding["branch"]
    assert verify.is_ancestor(case.remote, binding["head"], unit.evidence["merge"]["merge_commit"])
    assert case.service.issues[unit.bindings["issue"]["number"]]["state"] == "closed"
    assert case.service.pulls[pr["number"]]["body"] == pr["body"]
    assert host.workspaces[0] == tombstone
    assert not workspace.exists()
    assert store.load(old.id) == retirement
    assert read_release(store, old.id, unit_id) == released["record"]
    assert sum(args[:2] == ["workspaces", "create"] for args in host.calls) == 2
