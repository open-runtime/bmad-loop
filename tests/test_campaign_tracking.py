"""The hierarchy projection executes through persisted, idempotent step receipts."""

from dataclasses import replace

from bmad_loop.campaign_github import ApiResponse, GithubError, GithubGateway
from bmad_loop.campaign_model import CampaignSnapshot, CampaignSpec, CampaignUnit, UnitSpec
from bmad_loop.campaign_tracking import HierarchyCoordinator


class HierarchyTransport:
    def __init__(self):
        self.milestones = {}
        self.issues = {}
        self.parents = {}
        self.effects = []

    def request(self, method, path, payload=None):
        route = path.split("?", 1)[0].removeprefix("repos/a/b/")
        if method == "GET":
            if route == "milestones":
                return ApiResponse(200, list(self.milestones.values()), {})
            if route == "issues":
                return ApiResponse(200, list(self.issues.values()), {})
            parts = route.split("/")
            if parts[0] == "milestones" and len(parts) == 2:
                return ApiResponse(200, self.milestones[int(parts[1])], {})
            if parts[0] == "issues":
                number = int(parts[1])
                if len(parts) == 2:
                    return ApiResponse(200, self.issues[number], {})
                if parts[-1] == "parent":
                    if number not in self.parents:
                        raise GithubError("No parent", status=404)
                    return ApiResponse(200, self.issues[self.parents[number]], {})
                if parts[-1] == "sub_issues":
                    return ApiResponse(
                        200,
                        [
                            issue
                            for n, issue in self.issues.items()
                            if self.parents.get(n) == number
                        ],
                        {},
                    )
        self.effects.append((method, route, payload))
        if method == "POST" and route == "milestones":
            number = len(self.milestones) + 1
            row = {
                "id": 9000 + number,
                "number": number,
                "html_url": f"https://github.com/a/b/milestone/{number}",
                "state": "open",
                "updated_at": "2026-01-01T00:00:00Z",
                **payload,
            }
            self.milestones[number] = row
            return ApiResponse(201, row, {})
        if method == "POST" and route == "issues":
            number = len(self.issues) + 1
            row = {
                "id": 1000 + number,
                "number": number,
                "node_id": f"node-{number}",
                "updated_at": "t1",
                "html_url": f"https://github.com/a/b/issues/{number}",
                "state": "open",
                **payload,
            }
            row["milestone"] = self.milestones.get(payload.get("milestone"))
            self.issues[number] = row
            return ApiResponse(201, row, {})
        if method == "PATCH":
            parts = route.split("/")
            row = (self.milestones if parts[0] == "milestones" else self.issues)[int(parts[1])]
            row.update(payload)
            return ApiResponse(200, row, {})
        if method == "POST" and route.endswith("/sub_issues"):
            parent = int(route.split("/")[1])
            child = next(
                row for row in self.issues.values() if row["id"] == payload["sub_issue_id"]
            )
            self.parents[child["number"]] = parent
            return ApiResponse(201, child, {})
        raise AssertionError((method, path, payload))


def snapshot():
    spec = UnitSpec(
        "tickets/initiative/epic/story",
        "tickets",
        "epic",
        "Story",
        initiative="initiative",
        acceptance=("Acceptance",),
    )
    return CampaignSnapshot(
        "campaign", "a/b", CampaignSpec("Campaign"), {spec.id: CampaignUnit(spec)}, generation=1
    )


def test_hierarchy_step_retries_use_same_native_objects_and_preserve_human_content():
    state = snapshot()
    transport = HierarchyTransport()
    gateway = GithubGateway(transport, "a/b")
    coordinator = HierarchyCoordinator(gateway)
    receipts = {}
    unit_id = next(iter(state.units))
    for _ in range(20):
        step = coordinator.step(state, unit_id, receipts=receipts)
        state.bindings["github_hierarchy"] = step.bindings
        state.units[unit_id].bindings = step.unit_bindings
        if step.intent:
            receipts[step.action_key] = gateway.execute(
                replace(step.intent, state="sent_unknown"), generation=1
            )
        if step.complete:
            break
    assert step.complete
    assert len(transport.milestones) == 1
    assert len(transport.issues) == 2
    assert transport.parents == {2: 1}
    assert state.units[unit_id].bindings["issue"]["database_id"] == 1002
    effects = list(transport.effects)
    transport.issues[2]["body"] += "\nHuman detail"
    assert coordinator.step(state, unit_id, receipts=receipts).complete
    assert transport.effects == effects
    assert transport.issues[2]["body"].endswith("Human detail")


def test_existing_tracker_retains_its_epic_and_milestone_lineage():
    state = snapshot()
    transport = HierarchyTransport()
    gateway = GithubGateway(transport, "a/b")
    milestone = {"id": 9007, "number": 7, "title": "Existing initiative", "state": "open"}
    transport.milestones[7] = milestone
    transport.issues[10] = {
        "id": 1010,
        "number": 10,
        "title": "Human epic",
        "milestone": milestone,
        "updated_at": "t0",
        "state": "open",
    }
    transport.issues[11] = {
        "id": 1011,
        "number": 11,
        "title": "Existing story",
        "milestone": milestone,
        "updated_at": "t0",
        "state": "open",
    }
    transport.parents[11] = 10
    unit_id = next(iter(state.units))
    coordinator = HierarchyCoordinator(gateway)
    for _ in range(10):
        step = coordinator.step(
            state,
            unit_id,
            source_metadata={unit_id: {"tracker_id": "https://github.com/a/b/issues/11"}},
        )
        state.bindings["github_hierarchy"] = step.bindings
        state.units[unit_id].bindings = step.unit_bindings
        if step.intent:
            assert step.intent.state == "confirmed"  # Parent read-back, no effect.
            state.bindings["github_hierarchy"]["story:" + unit_id]["parent_confirmed"] = True
        if step.complete:
            break
    assert step.complete
    assert state.units[unit_id].bindings["epic_issue"]["number"] == 10
    assert state.units[unit_id].bindings["milestone"]["number"] == 7
    assert transport.effects == []


def test_uncertain_create_receipt_is_a_hold_not_another_post():
    state = snapshot()
    coordinator = HierarchyCoordinator(GithubGateway(HierarchyTransport(), "a/b"))
    unit_id = next(iter(state.units))
    first = coordinator.step(state, unit_id)
    unknown = replace(first.intent, state="sent_unknown", error="lost response")
    second = coordinator.step(state, unit_id, receipts={first.action_key: unknown})
    assert second.intent is None
    assert second.reason == "lost response"
    assert not second.complete


def test_parent_closure_waits_for_all_children_and_closure_unit_delivered():
    from bmad_loop.campaign_model import UnitState

    state = snapshot()
    unit_id = next(iter(state.units))
    state.bindings["github_hierarchy"] = {
        "epic:tickets:initiative:epic": {"number": 10, "database_id": 1010, "kind": "issue"}
    }
    transport = HierarchyTransport()
    transport.issues[10] = {"id": 1010, "number": 10, "state": "open", "updated_at": "t0"}
    transport.issues[11] = {"id": 1011, "number": 11, "state": "open", "updated_at": "t0"}
    transport.parents[11] = 10
    coordinator = HierarchyCoordinator(GithubGateway(transport, "a/b"))
    waiting = coordinator.close_parents(state)
    assert waiting.complete
    assert waiting.intent is None  # Other campaign work is still running.
    state.units[unit_id].state = UnitState.DELIVERED
    waiting = coordinator.close_parents(state)
    assert waiting.reason == "Parent has open required or unmanaged native subissues"
    transport.issues[11]["state"] = "closed"
    ready = coordinator.close_parents(state)
    assert ready.intent.payload == {"state": "closed", "state_reason": "completed"}
    assert transport.effects == []


def projected():
    state = snapshot()
    transport = HierarchyTransport()
    gateway = GithubGateway(transport, "a/b")
    coordinator = HierarchyCoordinator(gateway)
    receipts = {}
    unit_id = next(iter(state.units))
    for _ in range(20):
        step = coordinator.step(state, unit_id, receipts=receipts)
        state.bindings["github_hierarchy"] = step.bindings
        state.units[unit_id].bindings = step.unit_bindings
        if step.intent:
            receipts[step.action_key] = gateway.execute(
                replace(step.intent, state="sent_unknown"), generation=1
            )
        if step.complete:
            return state, transport, gateway, coordinator, receipts, unit_id
    raise AssertionError("fixture projection did not converge")


def test_reviewed_scope_extension_reopens_existing_parent_without_recreating_it():
    from bmad_loop.campaign_model import UnitState

    state, transport, gateway, coordinator, receipts, unit_id = projected()
    transport.milestones[1].update(state="closed", updated_at="2026-01-01T00:00:00Z")
    plan = CampaignUnit(
        UnitSpec("planning", "campaign-plan", "planning", "Plan", kind="planning"),
        UnitState.DELIVERED,
        bindings={"pr": {"number": 90}},
        evidence={
            "planning_flow": {
                "activated_units": [unit_id],
                "stages": {"planning_review": {"finished_at": 1793929000}},
            }
        },
    )
    state.units[plan.id] = plan
    state.units[unit_id].bindings["planning_unit"] = plan.id
    state.graph_revision += 1
    step = coordinator.step(
        state, unit_id, receipts=receipts, source_metadata={unit_id: {"revision": "reviewed-main"}}
    )
    assert step.intent.kind == "reopen_milestone"
    assert step.intent.coordinates["approved_scope"]["planning_pr"] == 90
    result = gateway.execute(replace(step.intent, state="sent_unknown"), generation=1)
    receipts[step.action_key] = result
    assert result.state == "confirmed"
    assert transport.milestones[1]["state"] == "open"
    assert len(transport.milestones) == 1 and len(transport.issues) == 2


def test_closed_scope_without_reviewed_extension_is_held():
    state, transport, _, coordinator, receipts, unit_id = projected()
    transport.milestones[1].update(state="closed", updated_at="2026-01-01T00:00:00Z")
    before = list(transport.effects)
    step = coordinator.step(state, unit_id, receipts=receipts)
    assert step.intent is None and "reviewed extension authority" in step.reason
    assert transport.effects == before


def test_new_owner_closure_after_scope_review_is_not_reopened():
    from bmad_loop.campaign_model import UnitState

    state, transport, _, coordinator, receipts, unit_id = projected()
    transport.milestones[1].update(state="closed", updated_at="2026-11-06T00:00:00Z")
    plan = CampaignUnit(
        UnitSpec("planning", "campaign-plan", "planning", "Plan", kind="planning"),
        UnitState.DELIVERED,
        bindings={"pr": {"number": 90}},
        evidence={
            "planning_flow": {
                "activated_units": [unit_id],
                "stages": {"planning_review": {"finished_at": 1}},
            }
        },
    )
    state.units[plan.id] = plan
    state.units[unit_id].bindings["planning_unit"] = plan.id
    step = coordinator.step(
        state, unit_id, receipts=receipts, source_metadata={unit_id: {"revision": "reviewed-main"}}
    )
    assert step.intent is None and "owner-state" in step.reason
    assert transport.milestones[1]["state"] == "closed"
