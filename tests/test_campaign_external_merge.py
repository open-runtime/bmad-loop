"""Externally merged work retains exact acceptance and actual-landing obligations."""

import copy
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from conftest import git
from test_campaign_publication import publishable as publishable
from test_campaign_runtime import diagnose
from test_campaign_runtime import production_case as production_case
from test_campaign_runtime import run_case

from bmad_loop import campaign_external_merge, verify
from bmad_loop.campaign_external_merge import ExternalLandingEvidence, ExternalMergeEvidence
from bmad_loop.campaign_model import (
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    ResolutionSpec,
    UnitSpec,
    UnitState,
    WorkerClaim,
)
from bmad_loop.campaign_store import reconcile_external_merge
from bmad_loop.statemachine import IllegalTransition, check_campaign_external_merge


def test_typed_external_landing_has_no_fake_native_submission_states():
    spec = UnitSpec("source:project:u", "source", "e", "Unit")
    unit = CampaignUnit(
        spec,
        UnitState.READY_TO_PUBLISH,
        claim=WorkerClaim(spec.id, 1, 1, "review", quiescent=True),
        bindings={"source_revision": "s1"},
        evidence={
            "integrated": {"head": "h", "base": "b"},
            "verification": {"launch_id": "verify"},
            "review": {"launch_id": "review"},
        },
    )
    snapshot = CampaignSnapshot(
        "external", "a/b", CampaignSpec("External", units=(spec,)), {spec.id: unit}
    )
    proof = ExternalMergeEvidence(
        "a/b",
        spec.id,
        "h",
        "b",
        "main",
        "m",
        "c",
        ("b", "h"),
        ("h",),
        "verify",
        "review",
        "s1",
        1,
        1,
        True,
        True,
    )
    reconcile_external_merge(snapshot, unit.id, proof)
    assert unit.state == UnitState.MERGED_PENDING_SYNC
    assert [row["kind"] for row in snapshot.events] == ["external-merge-reconciled"]
    assert not [row for row in snapshot.events if row.get("target") == "merge_requested"]


@pytest.mark.parametrize(
    "phase", ["pending", "running", "locally_complete", "syncing", "reviewing", "delivered"]
)
def test_external_merge_reconciliation_cannot_bypass_acceptance(phase):
    with pytest.raises(IllegalTransition, match="ready-to-publish"):
        check_campaign_external_merge("unit", phase)


def test_canonical_merge_observation_retains_real_closed_state_and_native_remote_gates(publishable):
    from bmad_loop.campaign_publication import MergeReceipt

    controller, candidate, evidence, observation = publishable
    closed = copy.deepcopy(observation.pr)
    closed.update(state="closed", merged=True, merge_commit_sha="m" * 40, mergeable=None)
    observed = replace(observation, pr=closed, base_head="m" * 40)
    proof = MergeReceipt("merged_pending_sync", merge_commit="m" * 40, canonical_head="m" * 40)
    assert controller.evaluate(
        candidate, evidence, observed, independent_families=True, canonical_merge=proof
    ).ready
    held = replace(observed, issue={"labels": [{"name": "bmad:hold"}]})
    assert not controller.evaluate(
        candidate, evidence, held, independent_families=True, canonical_merge=proof
    ).ready
    assert closed["state"] == "closed" and closed["merged"] is True


class ExternalMerger:
    def __init__(self, service, *, uncovered=False, break_dependent=False):
        self.service, self.original = service, service.request
        self.uncovered = uncovered
        self.break_dependent = break_dependent
        self.broken = []
        self.merged = set()

    def request(self, method, path, payload=None):
        service = self.service
        with service.lock:
            urlsplit(path).path.removeprefix("repos/a/b/").split("/")
            if (
                method == "POST"
                and path == "graphql"
                and "markPullRequestReadyForReview" in payload.get("query", "")
            ):
                response = self.original(method, path, payload)
                node = payload["variables"]["input"]["pullRequestId"]
                pr = next(row for row in service.pulls.values() if row["node_id"] == node)
                number, head = pr["number"], pr["head"]["sha"]
                base = service._base()
                # Preserve concurrent product work. Inject uncovered ancestry
                # only after the candidate includes the actual current base.
                if not verify.is_ancestor(service.remote, base, head):
                    return response
                if self.uncovered:
                    tree = git(service.remote, "rev-parse", base + "^{tree}")
                    base = git(
                        service.remote,
                        "-c",
                        "user.name=External",
                        "-c",
                        "user.email=external@example.test",
                        "commit-tree",
                        tree,
                        "-p",
                        base,
                        "-m",
                        "unreviewed intervening canonical history",
                    )
                    git(service.remote, "update-ref", "refs/heads/main", base)
                tree = git(service.remote, "rev-parse", head + "^{tree}")
                if self.break_dependent and not self.broken and pr["title"].lower() == "dependent":
                    broken = service.state_file.parent / "broken-external-landing.txt"
                    broken.write_text("broken external landing\n")
                    blob = git(service.remote, "hash-object", "-w", str(broken))
                    git(service.remote, "read-tree", tree)
                    git(
                        service.remote,
                        "update-index",
                        "--cacheinfo",
                        "100644," + blob + ",src/dependent.txt",
                    )
                    tree = git(service.remote, "write-tree")
                    self.broken.append(number)
                merged = git(
                    service.remote,
                    "-c",
                    "user.name=External",
                    "-c",
                    "user.email=external@example.test",
                    "commit-tree",
                    tree,
                    "-p",
                    base,
                    "-p",
                    head,
                    "-m",
                    "External reviewed landing",
                )
                git(service.remote, "update-ref", "refs/heads/main", merged, base)
                pr.update(merged=True, state="closed", merge_commit_sha=merged)
                service.merge_bases[number] = base
                self.merged.add(number)
                service._save()
                return response
            return self.original(method, path, payload)


def test_external_ready_pr_merge_reconciles_canonical_delivery_without_native_merge_intent(
    production_case, monkeypatch
):
    from bmad_loop.campaign_action_history import actions

    case = production_case
    external = ExternalMerger(case.service)
    monkeypatch.setattr(case.service, "request", external.request)
    result = run_case(case)
    assert result.phase == "delivered", diagnose(result)
    assert external.merged
    reconciled = [row for row in result.events if row["kind"] == "external-merge-reconciled"]
    assert reconciled
    for row in reconciled:
        unit = result.units[row["unit"]]
        assert unit.state == UnitState.DELIVERED
        assert unit.evidence["external_merge"]["schema_version"] == 1
        assert unit.evidence["external_merge"]["covered_tree"] is True
        assert not [
            action
            for action in actions(result)
            if action.unit == unit.id and action.payload.get("purpose") == "merge"
        ]
        assert verify.is_ancestor(case.remote, row["head"], git(case.remote, "rev-parse", "main"))


@pytest.mark.parametrize("restart", [False, True])
def test_uncovered_external_merge_qualifies_actual_landing_before_tracking(
    production_case, monkeypatch, restart
):
    from bmad_loop.campaign_action_history import actions

    case = production_case
    external = ExternalMerger(case.service, uncovered=True)
    monkeypatch.setattr(case.service, "request", external.request)
    original_proposals = case.runtime.proposals
    pending = {}
    stop_requested = False

    def observe_landing(snapshot):
        nonlocal stop_requested
        for unit in snapshot.units.values():
            debt = unit.evidence.get("landing_acceptance", {})
            if unit.evidence.get("external_landing") and debt.get("state") == "pending":
                pending.setdefault(
                    unit.id,
                    {
                        key: copy.deepcopy(unit.bindings[key])
                        for key in ("workspace", "branch", "pr")
                    },
                )
                assert unit.state == UnitState.MERGED_PENDING_SYNC
                assert case.service.issues[unit.bindings["issue"]["number"]]["state"] == "open"
                assert not any(
                    row.unit == unit.id and row.kind == "sync-tracking" for row in actions(snapshot)
                )
                if restart and not stop_requested:
                    stop_requested = True
                    case.store.enqueue(snapshot.id, "stop", request_id="observed-landing-debt")
        return original_proposals(snapshot)

    monkeypatch.setattr(case.runtime, "proposals", observe_landing)
    result = run_case(case)
    if restart:
        assert stop_requested and result.phase == "stopped", diagnose(result)
        assert any(
            row.evidence.get("external_landing") and row.state == UnitState.MERGED_PENDING_SYNC
            for row in result.units.values()
        ), diagnose(result)
        result = run_case(case)
    assert result.phase == "delivered", diagnose(result)
    assert pending
    for key, bindings in pending.items():
        unit = result.units[key]
        proof = unit.evidence["external_landing"]
        debt = unit.evidence["landing_acceptance"]
        assert proof["schema_version"] == 2 and proof["covered_tree"] is False
        assert proof["pr_number"] == bindings["pr"]["number"]
        assert proof["merge_tree"] == git(
            case.remote, "rev-parse", proof["merge_commit"] + "^{tree}"
        )
        assert debt["state"] == "accepted" and debt["acceptance"]["complete"]
        assert debt["verification"]["head"] == debt["review"]["head"] == proof["merge_commit"]
        assert proof["head"] == unit.evidence["integrated"]["head"] != proof["merge_commit"]
        assert all(unit.bindings[name] == value for name, value in bindings.items())
        assert (
            unit.state == UnitState.DELIVERED
            and unit.evidence["source_publication"]["state"] == "confirmed"
        )
        assert case.service.issues[unit.bindings["issue"]["number"]]["state_reason"] == "completed"
        assert not [
            action
            for action in actions(result)
            if action.unit == key and action.payload.get("purpose") == "merge"
        ]


def test_failed_external_landing_delivers_one_linked_repair_before_original_tracking(
    production_case, monkeypatch
):
    from bmad_loop.campaign_action_history import actions

    case = production_case
    snapshot = case.store.load(case.snapshot.id)
    template = case.runtime.template(snapshot)
    snapshot.spec = replace(snapshot.spec, resolution=ResolutionSpec(mode="bounded-auto"))
    snapshot.config_revision += 1
    path, digest = replace(
        template,
        config_revision=snapshot.config_revision,
        directory=template.directory.parent / "configuration-2",
    ).save()
    snapshot.bindings["execution_template"] = {"path": str(path), "digest": digest}
    case.store.save(snapshot, snapshot.revision)
    original = snapshot.units["source:project:dependent"]
    obligations = original.spec.acceptance
    script = case.events.parent / "scripted-coding-cli.py"
    content = script.read_text()
    assert '"publication_review","admission_review","requirements"' in content
    content = content.replace(
        '"publication_review","admission_review","requirements"',
        '"landing_repair","publication_review","admission_review","requirements"',
    ).replace(
        '    if name=="publication_review":',
        '    if name=="landing_repair":\n'
        '        workspace=Path(request["workspace"])\n'
        "        changed=[]\n"
        '        for relative in request["unit"]["scopes"]:\n'
        "            path=workspace/relative\n"
        '            path.write_text("accepted repaired external landing\\n")\n'
        "            changed.append(path)\n"
        '        verify.commit_paths(workspace,"fix: repair external landing",changed)\n'
        '    if name=="publication_review":',
    )
    script.write_text(content)
    external = ExternalMerger(case.service, uncovered=True, break_dependent=True)
    monkeypatch.setattr(case.service, "request", external.request)
    original_proposals = case.runtime.proposals
    pending_repair = []

    def observe_repair(snapshot):
        unit = snapshot.units[original.id]
        debt = unit.evidence.get("landing_acceptance", {})
        if debt.get("repair_unit") and debt.get("state") != "accepted":
            pending_repair.append(debt["repair_unit"])
            assert unit.state == UnitState.MERGED_PENDING_SYNC
            assert case.service.issues[unit.bindings["issue"]["number"]]["state"] == "open"
            assert not any(
                action.unit == unit.id and action.kind == "sync-tracking"
                for action in actions(snapshot)
            )
        return original_proposals(snapshot)

    monkeypatch.setattr(case.runtime, "proposals", observe_repair)
    result = run_case(case)
    assert result.phase == "delivered", diagnose(result)
    unit = result.units[original.id]
    debt = unit.evidence["landing_acceptance"]
    proof = unit.evidence["external_landing"]
    assert external.broken == [unit.bindings["pr"]["number"]]
    assert debt["state"] == "accepted"
    assert debt["failure"]["head"] == proof["merge_commit"]
    assert pending_repair and set(pending_repair) == {debt["repair_unit"]}
    repair = result.units[debt["repair_unit"]]
    assert repair.spec.kind == "landing_repair" and repair.spec.parent == unit.id
    assert unit.bindings["landing_repair_units"] == [repair.id]
    assert repair.spec.acceptance == unit.spec.acceptance == obligations
    assert repair.state == UnitState.DELIVERED
    assert debt["repaired"]["acceptance"] == repair.evidence["acceptance"]["current"]
    assert debt["repaired"]["acceptance"]["complete"]
    assert repair.bindings["pr"]["number"] != unit.bindings["pr"]["number"]
    assert "accepted repaired external landing" in git(
        case.remote, "show", "main:src/dependent.txt"
    )
    assert len([row for row in result.units.values() if row.spec.kind == "landing_repair"]) == 1
    assert unit.evidence["source_publication"]["state"] == "confirmed"
    assert case.service.issues[unit.bindings["issue"]["number"]]["state_reason"] == "completed"
    assert not any(
        action.unit == unit.id and action.payload.get("purpose") == "merge"
        for action in actions(result)
    )


@pytest.fixture
def uncovered_candidate(project, monkeypatch, publishable):
    from bmad_loop.campaign_publication import MergeReceipt, PublicationController

    controller, original, accepted, observation = publishable
    repo = project.project
    base = git(repo, "rev-parse", "HEAD")
    (repo / "product.txt").write_text("accepted product")
    git(repo, "add", "product.txt")
    git(repo, "commit", "-qm", "accepted product")
    head = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", head + "^{tree}")
    newer = git(repo, "commit-tree", tree, "-p", base, "-m", "intervening canonical history")
    merge = git(repo, "commit-tree", tree, "-p", newer, "-p", head, "-m", "external merge")
    candidate = replace(original, head=head, base=base, config_revision="1")
    evidence = replace(accepted, candidate=candidate)
    observed = replace(
        observation,
        pr={
            **observation.pr,
            "head": {"sha": head},
            "merged": True,
            "state": "closed",
            "merge_commit_sha": merge,
        },
        base_head=merge,
        checks=tuple({**row, "head_sha": head} for row in observation.checks),
        reviews=tuple({**row, "commit_id": head} for row in observation.reviews),
    )
    unit = CampaignUnit(
        UnitSpec("unit", "s", "e", "Unit"),
        state=UnitState.READY_TO_PUBLISH,
        claim=WorkerClaim("unit", 1, 1, "review", quiescent=True),
        bindings={"source_revision": "s1", "pr": {"number": 1}},
        evidence={
            "integrated": {"head": head, "base": base},
            "verification": {"outcome": "verified", "head": head, "launch_id": "verify"},
            "review": {
                "launch_id": "review",
                "validated": {
                    "verdict": "approved",
                    "coordinates": {
                        "head": head,
                        "base": base,
                        "source_revision": "s1",
                        "graph_revision": 1,
                        "config_revision": 1,
                    },
                },
            },
        },
    )
    snapshot = CampaignSnapshot(
        "external", "a/b", CampaignSpec("External", units=(unit.spec,)), {unit.id: unit}
    )
    state = SimpleNamespace(
        observed=observed,
        evidence=evidence,
        checkpoints=(SimpleNamespace(head=head, state="confirmed"),),
    )
    runtime = SimpleNamespace(
        project=repo,
        clock=lambda: 100,
        gateway=lambda _: controller.gateway,
        _candidate=lambda *_: candidate,
        _publication_evidence=lambda *_: state.evidence,
        _pusher=lambda *_: SimpleNamespace(observations=lambda: state.checkpoints),
    )
    receipt = MergeReceipt("merged_pending_sync", merge_commit=merge, canonical_head=merge)
    state.receipt = receipt
    monkeypatch.setattr(
        PublicationController, "merge_status", lambda *args, **kwargs: state.receipt
    )
    monkeypatch.setattr(
        PublicationController, "confirm_canonical", lambda *args, **kwargs: state.receipt
    )
    monkeypatch.setattr(PublicationController, "inspect", lambda *args: state.observed)
    return runtime, snapshot, unit, state, candidate, tree


@pytest.mark.parametrize(
    "defect",
    [
        None,
        "head_history",
        "checkpoint",
        "restored_head",
        "restored_checkpoint",
        "moved_target",
        "verification",
        "review",
        "source",
        "graph",
        "config",
        "quiescence",
        "hold",
        "check",
        "thread",
        "requested_review",
        "family",
    ],
)
def test_uncovered_external_landing_requires_exact_acceptance_and_remote_gates(
    uncovered_candidate, monkeypatch, defect
):
    runtime, snapshot, unit, state, candidate, tree = uncovered_candidate
    baseline = campaign_external_merge.inspect(runtime, snapshot, unit, state.observed)
    assert baseline.data["external_landing"]["merge_tree"] == tree
    coordinates = unit.evidence["review"]["validated"]["coordinates"]
    if defect == "head_history":
        ancestor = verify.is_ancestor
        monkeypatch.setattr(
            verify,
            "is_ancestor",
            lambda repo, head, target: (
                False if head == candidate.head else ancestor(repo, head, target)
            ),
        )
    elif defect == "checkpoint":
        omitted = git(runtime.project, "commit-tree", tree, "-m", "omitted checkpoint")
        state.checkpoints = (SimpleNamespace(head=omitted, state="sent_unknown"),)
    elif defect in {"restored_head", "restored_checkpoint"}:
        merge = state.receipt.merge_commit
        restored = candidate.head
        if defect == "restored_head":
            merge = git(
                runtime.project, "commit-tree", tree, "-p", candidate.base, "-m", "squashed product"
            )
        else:
            restored = git(runtime.project, "commit-tree", tree, "-m", "checkpoint")
            state.checkpoints = (SimpleNamespace(head=restored, state="sent_unknown"),)
        canonical = git(
            runtime.project,
            "commit-tree",
            tree,
            "-p",
            merge,
            "-p",
            restored,
            "-m",
            "later history restoration",
        )
        assert verify.is_ancestor(runtime.project, restored, canonical)
        assert not verify.is_ancestor(runtime.project, restored, merge)
        state.receipt = replace(state.receipt, merge_commit=merge, canonical_head=canonical)
        state.observed = replace(
            state.observed, base_head=canonical, pr={**state.observed.pr, "merge_commit_sha": merge}
        )
    elif defect == "moved_target":
        state.observed = replace(state.observed, base_head="d" * 40)
    elif defect == "verification":
        unit.evidence["verification"]["head"] = candidate.base
    elif defect == "review":
        coordinates["head"] = candidate.base
    elif defect in {"source", "graph", "config"}:
        coordinates[defect + "_revision"] = "stale" if defect == "source" else 0
    elif defect == "quiescence":
        unit.claim.quiescent = False
    elif defect == "hold":
        state.observed = replace(state.observed, issue={"labels": [{"name": "bmad:hold"}]})
    elif defect == "check":
        state.observed = replace(state.observed, checks=())
    elif defect == "thread":
        state.observed = replace(state.observed, threads=({"id": "thread", "isResolved": False},))
    elif defect == "requested_review":
        state.observed = replace(state.observed, requested_teams=("security",))
    elif defect == "family":
        state.evidence = replace(state.evidence, reviewer_family=state.evidence.developer_family)
    result = campaign_external_merge.inspect(runtime, snapshot, unit, state.observed)
    if defect == "moved_target":
        assert result.state == "waiting" and "target moved" in result.reason
        assert "external_landing" not in result.data
    elif defect:
        assert result.data.get("external_followup_hold")
        assert "external_landing" not in result.data
    else:
        proof = ExternalLandingEvidence(**result.data["external_landing"])
        assert proof.schema_version == 2 and not proof.covered_tree
        assert proof.merge_tree == tree and proof.pr_number == candidate.pr_number
        assert proof.verification_launch == "verify" and proof.review_launch == "review"
        assert unit.state == UnitState.READY_TO_PUBLISH
