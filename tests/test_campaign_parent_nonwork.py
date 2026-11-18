"""Only an unchanged, natively canceled intake control may settle without a product review."""

import copy
from dataclasses import replace

import pytest
from test_campaign_parent_closure import submit
from test_campaign_runtime_intake import apply_existing_duplicate, result
from test_campaign_runtime_intake import snapshot as snapshot
from test_campaign_runtime_intake import start
from test_campaign_tracking import HierarchyTransport

from bmad_loop import campaign_runtime_intake as intake
from bmad_loop.campaign_acceptance import ensure
from bmad_loop.campaign_completion_scope import all_members_settled, parent_wait_reason
from bmad_loop.campaign_github import ApiResponse, GithubGateway
from bmad_loop.campaign_model import Hold, OutboxAction, UnitState
from bmad_loop.campaign_parent_closure import proposals
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_store import CampaignStore
from bmad_loop.campaign_supervisor import CampaignSupervisor
from bmad_loop.campaign_tracking import HierarchyCoordinator


def nonwork(snapshot, tmp_path, *, disposition="attach"):
    product = next(iter(snapshot.units.values()))
    if disposition == "duplicate":
        key = apply_existing_duplicate(snapshot)
    else:
        key = start(snapshot)
        intake.apply_triage(
            snapshot, key, result(snapshot, key, disposition="attach"), launch_id="triage-launch"
        )
    product.state = UnitState.DELIVERED
    ledger = ensure(product)
    ledger["current"] = {
        "complete": True,
        "revision": ledger["revision"],
        "criteria": [{**c, "status": "passed"} for c in ledger["criteria"]],
    }
    unit = snapshot.units[key]
    unit.bindings["workspace"] = str(tmp_path / "control")
    unit.bindings["pr"]["node_id"] = "P20"
    bootstrap = {k: unit.bindings[k] for k in ("workspace", "branch", "head")}
    snapshot.outbox.append(
        OutboxAction(
            "original", "prepare-unit", key, {}, snapshot.generation, "confirmed", bootstrap
        )
    )
    ensure(unit)
    intake.apply_note(snapshot, key, {"state": "confirmed", "receipt": {"id": 22}})
    intake.apply_issue_cancellation(
        snapshot,
        key,
        {
            "kind": "close_issue_not_planned",
            "state": "confirmed",
            "coordinates": {"disposition_id": "triage-launch"},
            "receipt": {"id": 2019, "number": 19, "state": "closed", "state_reason": "not_planned"},
        },
    )
    cancel = {
        "kind": "close_pr",
        "state": "confirmed",
        "receipt": {
            "id": 2020,
            "number": 20,
            "node_id": "P20",
            "state": "closed",
            "merged": False,
            "head": {"sha": unit.bindings["head"], "ref": unit.bindings["branch"]},
        },
    }
    snapshot.outbox.append(
        OutboxAction(
            "cancel",
            "github",
            key,
            {"purpose": "intake-cancel"},
            snapshot.generation,
            "confirmed",
            {"intent": cancel},
        )
    )
    intake.apply_cancellation(snapshot, key, cancel)
    parent = "epic:campaign-intake:intake:intake"
    snapshot.bindings["github_hierarchy"] = {
        parent: {"kind": "issue", "database_id": 2001, "number": 1}
    }
    return unit, parent


@pytest.mark.parametrize("disposition", ["attach", "duplicate"])
def test_proved_nonwork_control_can_settle_only_its_native_parent(snapshot, tmp_path, disposition):
    unit, parent = nonwork(snapshot, tmp_path, disposition=disposition)
    original = copy.deepcopy(unit.evidence["acceptance"])
    assert intake.optional_canceled(unit) and parent_wait_reason(snapshot, parent) == ""
    assert proposals(snapshot)[0].payload == {"parent_key": parent}
    assert unit.evidence["acceptance"] == original
    assert all(row["status"] == "not_run" for row in original["criteria"])


def test_legacy_unbound_duplicate_stays_historical_without_current_parent_authority(
    snapshot, tmp_path
):
    unit, parent = nonwork(snapshot, tmp_path, disposition="duplicate")
    value = unit.bindings["intake_control"]
    inbox = snapshot.bindings["intake_inbox"]
    for original in [
        value["disposition"],
        inbox["attempts"][value["attempt_id"]]["disposition"],
        *(inbox["records"][key]["disposition"] for key in value["input_versions"]),
    ]:
        original.pop("duplicate_target", None)
        original.pop("duplicate_proof", None)
    before = copy.deepcopy(value["disposition"])
    assert "duplicate target identity" in parent_wait_reason(snapshot, parent)
    assert not proposals(snapshot) and value["disposition"] == before
    from bmad_loop.campaign_cli import inbox_document, status_document

    (row,) = [
        item
        for item in inbox_document(snapshot, unit=unit.id)["items"]
        if item["kind"] == "completion_wait"
    ]
    assert row["operator_action_required"] and "duplicate target identity" in row["reason"]
    assert "reviewed intake disposition" in row["operator_action"] and row["retry_at"] is None
    public = next(item for item in status_document(snapshot)["units"] if item["id"] == unit.id)
    assert public["completion_wait"] == row["reason"] and public["hold"] is None


def test_nonwork_parent_lifecycle_confirms_two_native_closures_without_product_acceptance(
    snapshot, tmp_path
):
    unit, epic = nonwork(snapshot, tmp_path)

    class NativeHierarchy(HierarchyTransport):
        def request(self, method, path, payload=None):
            from urllib.parse import parse_qs, urlsplit

            parsed = urlsplit(path)
            query = parse_qs(parsed.query)
            if method == "GET" and parsed.path == "repos/a/b/issues" and "milestone" in query:
                return ApiResponse(
                    200,
                    [
                        row
                        for row in self.issues.values()
                        if row["state"] == query["state"][0]
                        and row["milestone"]["number"] == int(query["milestone"][0])
                    ],
                    {},
                )
            return super().request(method, path, payload)

    transport = NativeHierarchy()
    transport.milestones[2] = {
        "id": 9002,
        "number": 2,
        "state": "open",
        "updated_at": "t0",
        "html_url": "https://github.com/a/b/milestone/2",
    }
    transport.issues[1] = {
        "id": 2001,
        "number": 1,
        "state": "open",
        "updated_at": "t0",
        "body": "Intake epic",
        "html_url": "https://github.com/a/b/issues/1",
        "labels": [],
    }
    transport.issues[19] = {
        "id": 2019,
        "number": 19,
        "state": "closed",
        "state_reason": "not_planned",
        "updated_at": "t0",
        "body": "Nonwork control",
        "html_url": "https://github.com/a/b/issues/19",
        "labels": [],
    }
    transport.parents[19] = 1
    for item in transport.issues.values():
        item["milestone"] = transport.milestones[2]
    initiative = "initiative:campaign-intake:intake"
    snapshot.bindings["github_hierarchy"].update(
        {
            epic: HierarchyCoordinator._binding(transport.issues[1], "issue"),
            initiative: HierarchyCoordinator._binding(transport.milestones[2], "milestone"),
        }
    )
    store = CampaignStore(tmp_path / "authority")
    state = store.create(snapshot)
    runtime = CampaignRuntime(
        tmp_path, store, gateway=GithubGateway(transport, "a/b"), clock=lambda: 100.0
    )
    supervisor = CampaignSupervisor(store, runtime, clock=runtime.clock)
    for _ in range(9):
        if state.bindings.get("hierarchy_finalized_graph") == state.graph_revision:
            break
        state = supervisor._queue(state, proposals(state))
        action = next(
            row for row in state.outbox if row.kind == "hierarchy-close" and row.state == "prepared"
        )
        state = submit(supervisor, state, action)
        for write in list(state.outbox):
            if write.kind == "github" and write.state == "prepared":
                state = submit(supervisor, state, write)
    assert runtime.completion_ready(state)
    assert all(
        state.bindings["github_hierarchy"][key]["closed_confirmed"] for key in (epic, initiative)
    )
    assert [(method, path) for method, path, _ in transport.effects] == [
        ("PATCH", "issues/1"),
        ("PATCH", "milestones/2"),
    ]
    assert state.units[unit.id].state == UnitState.CANCELED
    assert all(
        row["status"] == "not_run"
        for row in state.units[unit.id].evidence["acceptance"]["criteria"]
    )


@pytest.mark.parametrize(
    "change",
    [
        "approval",
        "owner",
        "live",
        "ledger",
        "spec",
        "finding",
        "branch",
        "native-pr",
        "native-issue",
        "unknown-disposition",
        "amendment",
    ],
)
def test_unsettled_work_or_authority_cannot_use_nonwork_exception(snapshot, tmp_path, change):
    unit, parent = nonwork(snapshot, tmp_path)
    assert parent_wait_reason(snapshot, parent) == ""
    value = unit.bindings["intake_control"]
    if change == "approval":
        unit.evidence["unresolved_approval_requirements"] = ["Owner signoff outstanding"]
    elif change == "owner":
        unit.hold = Hold("operator", "Pending owner investigation")
    elif change == "live":
        unit.claim.quiescent = False
    elif change == "ledger":
        unit.evidence["acceptance"]["criteria"].append(
            {"id": "new", "text": "Unique work", "required": True}
        )
    elif change == "spec":
        unit.spec = replace(unit.spec, acceptance=(*unit.spec.acceptance, "Unique work"))
    elif change == "finding":
        unit.evidence["required_findings"] = [{"required": True, "resolved": False}]
    elif change == "branch":
        unit.bindings["head"] = value["cancellation"]["head"] = "new-commit"
        snapshot.outbox[-1].receipt["intent"]["receipt"]["head"]["sha"] = "new-commit"
    elif change == "native-pr":
        value["cancellation"]["native_id"] += 1
    elif change == "native-issue":
        value["issue_cancellation"]["receipt"]["id"] += 1
    elif change == "unknown-disposition":
        value["attempt_id"] = "unrelated"
    else:
        value["proposal_id"] = "pending-reviewed-source"
    assert parent_wait_reason(snapshot, parent)
    assert proposals(snapshot) == () and not all_members_settled(snapshot)


@pytest.mark.parametrize("late", ["observation", "apply", "completion"])
@pytest.mark.parametrize("target", ["control", "product"])
def test_late_obligation_fences_aggregate_parent_completion(snapshot, tmp_path, late, target):
    unit, parent = nonwork(snapshot, tmp_path)
    snapshot.bindings["github_hierarchy"][parent]["closed_confirmed"] = True
    snapshot.bindings["intake"]["observed_at"] = 100
    store = CampaignStore(tmp_path / "state")
    original = store.create(snapshot)
    runtime = CampaignRuntime(tmp_path, store, gateway=object())
    assert all_members_settled(original)
    action = OutboxAction("final", "hierarchy-close", unit.id, {}, original.generation)
    result = runtime._perform_hierarchy_close(original, action)
    assert result.data["complete"]
    saved = store.load(original.id)
    if target == "control":
        saved.units[unit.id].evidence["unresolved_approval_requirements"] = ["New owner signoff"]
    else:
        product = saved.units["source:project:existing"]
        product.evidence["acceptance"]["criteria"].append(
            {"id": "new", "text": "Unproved product behavior", "required": True}
        )
    if late == "completion":
        saved.bindings["hierarchy_finalized_graph"] = saved.graph_revision
    saved = store.save(saved, saved.revision)
    assert not proposals(saved)
    if late == "observation":
        assert runtime._perform_hierarchy_close(original, action).data.get("stale_scope") is True
    elif late == "apply":
        runtime.apply(saved, action, result)
        assert "hierarchy_finalized_graph" not in saved.bindings
    assert not runtime.completion_ready(saved)
