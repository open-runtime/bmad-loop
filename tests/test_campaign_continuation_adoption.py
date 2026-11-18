"""Persisted successor adoption and retained ticket development, with scripted adapters."""

import copy
import dataclasses
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_campaign_continuation import development_handoff as development_handoff
from test_campaign_continuation import review_handoff as review_handoff

from bmad_loop.campaign_continuation import prepare_permit
from bmad_loop.campaign_model import Hold, OutboxAction, SourceSelector, UnitState, WorkerClaim
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_store import CampaignError, CampaignStore, consume_continuation_permit


@pytest.fixture
def adoption(development_handoff, tmp_path):
    project, old, adapter, child, request = development_handoff
    existing = CampaignStore(Path(request["continuation_authority"]))
    previous = existing.load(child.campaign_id)
    store = CampaignStore(
        tmp_path / "adoption", storage="json" if isinstance(previous.events, list) else "sqlite"
    )
    child = dataclasses.replace(
        child, directory=store.directory(child.campaign_id) / "workers" / request["launch_id"]
    )
    request = copy.deepcopy(request)
    selected = child.policy.adapter.resolved("dev")
    request["candidate"] = {
        "profile": selected.name,
        "model": selected.model,
        "effort": selected.effort,
    }
    request["continuation_authority"] = str(store.root)
    unit = previous.units[child.unit_id]
    predecessor = request["native_predecessor"]
    unit.claim = WorkerClaim(
        unit.id,
        predecessor["attempt"],
        predecessor["generation"],
        predecessor["launch_id"],
        run_id=predecessor["run_id"],
        run_dir=predecessor["run_dir"],
        workspace=predecessor["workspace"],
        branch=predecessor["branch"],
        quiescent=True,
    )
    request["native_predecessor"] = dataclasses.asdict(unit.claim)
    unit.attempt = unit.claim.attempt
    unit.state = UnitState.READY
    unit.hold = Hold("handoff", "ready continuation", id=request["handoff_id"])
    unit.bindings.update(
        workspace=str(project.project), branch=request["branch"], effective_source=request["source"]
    )
    unit.evidence.pop("continuation_permits")
    unit.evidence.update(
        dev_attempts_spent=2,
        review_cycles_spent=3,
        developer_families=["anthropic", "openai"],
        developer_attribution_incomplete=True,
    )
    unit.failure_attempts = 2
    unit.resolution_attempts = 1
    unit.review_attempts = 1
    h = request["handoff_state"]
    unit.evidence["handoff"] = {
        "id": request["handoff_id"],
        "phase": "ready",
        "role": "execution",
        "native_role": "dev",
        "candidate": request["candidate"],
        "predecessor": request["native_predecessor"],
        "capture": {
            "capture_digest": h["capture_digest"],
            "current": {k: v for k, v in h.items() if k not in {"capture_digest", "native_role"}},
        },
    }
    policy = dataclasses.replace(
        child.policy,
        limits=dataclasses.replace(child.policy.limits, max_dev_attempts=3, max_review_cycles=4),
    )
    template = dataclasses.replace(child, policy=policy, directory=tmp_path / "adoption-template")
    template_path, template_digest = template.save()
    previous.bindings["execution_template"] = {
        "path": str(template_path),
        "digest": template_digest,
    }
    previous.spec = dataclasses.replace(
        previous.spec, sources=(SourceSelector(**request["source"]),)
    )
    previous.config_snapshot = {"policy": policy.to_dict()}
    previous.common_dir = request["common_dir"]
    previous = store.create(previous)
    current = previous.clone()
    unit = current.units[child.unit_id]
    unit.attempt += 1
    unit.state = UnitState.CLAIMED
    unit.claim = WorkerClaim(
        unit.id,
        unit.attempt,
        current.generation,
        request["launch_id"],
        run_id=request["run_id"],
        run_dir=str(child.directory),
        workspace=str(project.project),
        branch=request["branch"],
        writes=True,
    )
    unit.evidence["handoff"].update(phase="continuing", successor=dataclasses.asdict(unit.claim))
    action = OutboxAction(
        request["launch_id"],
        "launch",
        unit.id,
        {
            "kind": "execution",
            "role": "execution",
            "candidate": request["candidate"],
            "handoff_id": request["handoff_id"],
        },
        current.generation,
    )
    current.outbox.append(action)
    runtime = object.__new__(CampaignRuntime)
    runtime.store = store
    runtime.project = project.project
    runtime.clock = lambda: 100
    envelope = runtime._launch_envelope(current, action)
    from bmad_loop.campaign_worker_authority import continuation_context

    request["continuation_context"] = continuation_context(current, unit)
    permit = prepare_permit(request, envelope)
    return SimpleNamespace(
        project=project,
        old=old,
        adapter=adapter,
        store=store,
        previous=previous,
        current=current,
        unit=unit,
        action=action,
        runtime=runtime,
        envelope=envelope,
        request=request,
        permit=permit,
    )


def rehash(permit):
    permit["permit_id"] = hashlib.sha256(
        json.dumps(permit["binding"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return permit


def test_runtime_mints_and_store_adopts_exact_successor(adoption):
    case = adoption
    case.runtime._consume_development_continuation(case.current, case.unit, case.action)
    assert case.action.payload["continuation_permit_id"] == case.permit["permit_id"]
    saved = case.store.save(case.current, case.previous.revision)
    record = saved.units[case.unit.id].evidence["continuation_permits"][case.permit["permit_id"]]
    assert record["permit"] == case.permit
    assert record["consumption"]["state"] == "consumed"
    assert saved.units[case.unit.id].claim.launch_id == case.request["launch_id"]
    assert saved.units[case.unit.id].evidence["dev_attempts_spent"] == 2
    with pytest.raises(CampaignError, match="already been consumed"):
        consume_continuation_permit(saved, case.unit.id, case.permit)


def test_changed_route_cannot_mint_authority_even_with_recomputed_permit(adoption):
    case = adoption
    assert case.action.payload["candidate"] == case.unit.evidence["handoff"]["candidate"]
    case.action.payload["candidate"] = {
        **case.action.payload["candidate"],
        "model": "unapproved-alternative",
    }
    changed_envelope = case.runtime._launch_envelope(case.current, case.action)
    changed_permit = prepare_permit(case.request, changed_envelope)
    assert changed_permit["permit_id"] != case.permit["permit_id"]
    with pytest.raises(CampaignError, match="approved handoff"):
        consume_continuation_permit(case.current, case.unit.id, changed_permit)


@pytest.mark.parametrize(
    "field",
    [
        "source",
        "task_digest",
        "configuration_digest",
        "original_state_sha256",
        "predecessor_receipt_sha256",
        "fingerprint",
        "remaining_dev_attempts",
        "remaining_review_cycles",
        "pr_number",
    ],
)
def test_rehashed_corrupt_permit_cannot_be_persisted_as_consumed(adoption, field):
    case = adoption
    permit = copy.deepcopy(case.permit)
    if field == "source":
        permit["binding"][field] = {**permit["binding"][field], "id": "foreign-source"}
    elif field == "fingerprint":
        permit["binding"][field] = {**permit["binding"][field], "head": "a" * 40}
    elif field.startswith("remaining_") or field == "pr_number":
        permit["binding"][field] += 1
    else:
        permit["binding"][field] = "0" * 64
    rehash(permit)
    with pytest.raises(CampaignError):
        consume_continuation_permit(case.current, case.unit.id, permit)
        case.store.save(case.current, case.previous.revision)


@pytest.mark.parametrize(
    "change",
    [
        "hold",
        "live_predecessor",
        "allowance_reset",
        "remove_record",
        "change_record",
        "new_successor",
        "contributors",
        "failure_debt",
        "resolution_debt",
        "review_debt",
        "unknown_attribution",
    ],
)
def test_store_preserves_consumed_authority_and_prior_owner(adoption, change):
    case = adoption
    consume_continuation_permit(case.current, case.unit.id, case.permit)
    if change in {
        "contributors",
        "failure_debt",
        "resolution_debt",
        "review_debt",
        "unknown_attribution",
    }:
        if change == "contributors":
            case.unit.evidence["developer_families"] = ["anthropic"]
        elif change == "unknown_attribution":
            case.unit.evidence["developer_attribution_incomplete"] = False
        else:
            setattr(
                case.unit,
                {
                    "failure_debt": "failure_attempts",
                    "resolution_debt": "resolution_attempts",
                    "review_debt": "review_attempts",
                }[change],
                0,
            )
        with pytest.raises(CampaignError):
            case.store.save(case.current, case.previous.revision)
        return
    if change in {"remove_record", "change_record", "new_successor"}:
        prior = case.store.save(case.current, case.previous.revision)
        changed = prior.clone()
        unit = changed.units[case.unit.id]
        if change == "remove_record":
            unit.evidence.pop("continuation_permits")
        elif change == "change_record":
            unit.evidence["continuation_permits"][case.permit["permit_id"]]["consumption"][
                "state"
            ] = "prepared"
        else:
            forged = copy.deepcopy(case.permit)
            forged["binding"]["successor"]["launch_id"] = "other-launch"
            rehash(forged)
            unit.evidence["continuation_permits"][forged["permit_id"]] = {
                "permit": forged,
                "consumption": {
                    **unit.evidence["continuation_permits"][case.permit["permit_id"]][
                        "consumption"
                    ],
                    "permit_id": forged["permit_id"],
                    "successor_launch_id": "other-launch",
                },
            }
        with pytest.raises(CampaignError):
            case.store.save(changed, prior.revision)
        return
    prior = case.store.load(case.previous.id)
    if change == "hold":
        prior.units[case.unit.id].hold = Hold("owner", "retain owner decision")
    elif change == "live_predecessor":
        prior.units[case.unit.id].claim.quiescent = False
    else:
        prior.units[case.unit.id].evidence["dev_attempts_spent"] = 3
    prior = case.store.save(prior, prior.revision)
    with pytest.raises(CampaignError):
        case.store.save(case.current, prior.revision)


def test_owned_inprogress_ticket_continues_dirty_work_through_review_and_commit(
    project_v7, tmp_path, monkeypatch
):
    from conftest import _file_exists_cmd, git, ticket_row
    from test_campaign_continuation import archive
    from test_tickets_engine import _bound, _marking_tree, _tree, _write_plan
    from test_tickets_engine import make_engine as make_tickets_engine

    from bmad_loop import runsetup, verify
    from bmad_loop.adapters.base import SessionResult
    from bmad_loop.adapters.mock import MockAdapter
    from bmad_loop.campaign_continuation import compose_continuation
    from bmad_loop.campaign_execution import CampaignExecution, bind_run
    from bmad_loop.campaign_finalization import _source_roots
    from bmad_loop.campaign_fingerprint import capture
    from bmad_loop.campaign_interruptions import execution_interruption
    from bmad_loop.campaign_model import CampaignSnapshot, CampaignSpec, CampaignUnit, UnitSpec
    from bmad_loop.engine import RunPaused
    from bmad_loop.journal import load_state, save_state
    from bmad_loop.model import PauseCause, PauseKind, Phase, SessionRecord, TokenUsage
    from bmad_loop.platform_util import root_identity_record
    from bmad_loop.policy import (
        GatesPolicy,
        LimitsPolicy,
        NotifyPolicy,
        Policy,
        ReviewPolicy,
        VerifyPolicy,
    )
    from bmad_loop.tickets_engine import TicketsEngine

    layout = project_v7
    repo = layout.project
    tree = _tree(layout, in_progress=(ticket_row("1.1", status="in-progress"),))
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        review=ReviewPolicy(enabled=True, trigger="always"),
        limits=LimitsPolicy(max_dev_attempts=3, max_review_cycles=2),
        notify=NotifyPolicy(desktop=False, file=False),
        verify=VerifyPolicy(commands=(_file_exists_cmd("completed.txt"),)),
    )
    old, _ = make_tickets_engine(layout, tree, policy=policy)
    with pytest.raises(RunPaused) as foreign:
        old._pick_next()
    assert foreign.value.cause.code == "foreign-active" and not old.state.tasks
    task = _bound(old, tree)
    plan = Path(task.ticket.plan)
    (repo / "initial-untracked.txt").write_text("owner residue\n")
    (repo / "owned.py").write_text("baseline\n")
    git(repo, "add", "owned.py")
    git(repo, "commit", "-qm", "original baseline")
    baseline = verify.rev_parse_head(repo)
    git(repo, "checkout", "-b", "campaign-ticket")
    _write_plan(plan, "ready-for-dev", baseline=baseline)
    preimage = plan.read_bytes()
    _write_plan(plan, "in-progress", baseline=baseline)
    (repo / "owned.py").write_text("published partial work\n")
    git(repo, "add", "owned.py")
    git(repo, "commit", "-qm", "partial checkpoint")
    checkpoint = verify.rev_parse_head(repo)
    (repo / "owned.py").write_text("staged partial work\n")
    git(repo, "add", "owned.py")
    (repo / "owned.py").write_text("unstaged partial work\n")
    (repo / "partial-untracked.txt").write_text("unfinished residue\n")
    task.phase = Phase.ESCALATED
    task.attempt = 1
    task.baseline_commit = baseline
    task.baseline_untracked = ["initial-untracked.txt"]
    task.spec_file = str(plan)
    task.dispatched_spec_file = str(plan)
    task.dispatched_spec_snapshot = preimage
    task.sessions = [
        SessionRecord("old-dev", "dev", "crashed", ended="pool", session_id="old-native")
    ]
    task.tokens = TokenUsage(input_tokens=17)
    original = CampaignExecution(
        "ticket-campaign",
        "tickets:project:1.1",
        1,
        1,
        1,
        tmp_path / "old-worker",
        policy,
        runsetup.resolve_profiles(policy, repo),
    )
    old.campaign_execution = original
    old.state.run_dir_identity = root_identity_record(old.run_dir)
    old.state.record_pause(
        "native quota",
        "escalation",
        task.story_key,
        cause=PauseCause(PauseKind.QUOTA_WAIT, "native-session", "pool"),
    )
    old.state.campaign_binding = bind_run(repo, old.run_dir, original)
    save_state(old.run_dir, old.state)
    state_path, state_sha = archive(tmp_path / "archive", old.state.to_dict())
    predecessor = WorkerClaim(
        original.unit_id,
        1,
        1,
        "old-launch",
        run_id=old.state.run_id,
        run_dir=str(original.directory),
        workspace=str(repo),
        branch="campaign-ticket",
        quiescent=True,
    )
    sessions = [r.to_dict() for r in task.sessions]
    receipt = {
        "campaign_id": original.campaign_id,
        "unit_id": original.unit_id,
        "attempt": 1,
        "generation": 1,
        "launch_id": "old-launch",
        "run_id": old.state.run_id,
        "run_dir": str(old.run_dir),
        "sessions": sessions,
        "interruption": execution_interruption(sessions),
        "outcome": "paused",
    }
    receipt_path, receipt_sha = archive(tmp_path / "archive", receipt)
    roots = _source_roots(repo, old.paths, old.state)
    handoff = {
        "capture_digest": "exact-ticket-capture",
        "native_role": "dev",
        "original_run_id": old.state.run_id,
        "original_run_dir": str(old.run_dir),
        "original_state_path": state_path,
        "original_state_sha256": state_sha,
        "predecessor_receipt_path": receipt_path,
        "predecessor_receipt_sha256": receipt_sha,
        "source_roots": [str(p.relative_to(repo)) for p in roots],
        "fingerprint": capture(repo, source_roots=roots).to_dict(),
    }
    store = CampaignStore(tmp_path / "ticket-authority")
    unit = CampaignUnit(
        UnitSpec(original.unit_id, "tickets", "1", "Owned ticket", selector="1.1"),
        UnitState.READY,
        attempt=1,
        claim=predecessor,
        bindings={"workspace": str(repo), "branch": "campaign-ticket", "pr": {"number": 9}},
        evidence={
            "dev_attempts_spent": 1,
            "review_cycles_spent": 0,
            "developer_families": ["anthropic"],
            "handoff": {
                "id": "ticket-handoff",
                "phase": "ready",
                "role": "execution",
                "native_role": "dev",
                "predecessor": dataclasses.asdict(predecessor),
                "capture": {
                    "capture_digest": handoff["capture_digest"],
                    "current": {
                        k: v
                        for k, v in handoff.items()
                        if k not in {"native_role", "capture_digest"}
                    },
                },
            },
        },
    )
    source = {"kind": "tickets", "id": "tickets", "initiative": layout.initiative}
    unit.bindings["effective_source"] = source
    snapshot = CampaignSnapshot(
        original.campaign_id,
        "owner/repo",
        CampaignSpec(
            "ticket continuation", sources=(SourceSelector(**source),), units=(unit.spec,)
        ),
        {unit.id: unit},
        generation=2,
        common_dir=str((repo / ".git").resolve()),
        config_snapshot={"policy": policy.to_dict()},
    )
    template = dataclasses.replace(original, directory=tmp_path / "ticket-template")
    template_path, template_digest = template.save()
    snapshot.bindings["execution_template"] = {
        "path": str(template_path),
        "digest": template_digest,
    }
    selected = policy.adapter.resolved("dev")
    candidate = {"profile": selected.name, "model": selected.model, "effort": selected.effort}
    unit.evidence["handoff"]["candidate"] = candidate
    previous = store.create(snapshot)
    snapshot = previous.clone()
    unit = snapshot.units[unit.id]
    directory = store.directory(snapshot.id) / "workers" / "new-ticket-launch"
    unit.attempt = 2
    unit.state = UnitState.CLAIMED
    unit.claim = WorkerClaim(
        unit.id,
        2,
        2,
        "new-ticket-launch",
        run_id="ticket-successor",
        run_dir=str(directory),
        workspace=str(repo),
        branch="campaign-ticket",
        writes=True,
    )
    unit.evidence["handoff"].update(phase="continuing", successor=dataclasses.asdict(unit.claim))
    selected = policy.adapter.resolved("dev")
    candidate = {"profile": selected.name, "model": selected.model, "effort": selected.effort}
    action = OutboxAction(
        "new-ticket-launch",
        "launch",
        unit.id,
        {
            "kind": "execution",
            "role": "execution",
            "candidate": candidate,
            "handoff_id": "ticket-handoff",
        },
        2,
    )
    snapshot.outbox.append(action)
    runtime = object.__new__(CampaignRuntime)
    runtime.project = repo
    runtime.store = store
    runtime.clock = lambda: 100
    runtime._consume_development_continuation(snapshot, unit, action)
    envelope = runtime._launch_envelope(snapshot, action)
    saved = store.save(snapshot, previous.revision)
    record = saved.units[unit.id].evidence["continuation_permits"][
        action.payload["continuation_permit_id"]
    ]
    request = {
        "workspace": str(repo),
        "branch": "campaign-ticket",
        "common_dir": snapshot.common_dir,
        "launch_id": action.id,
        "run_id": unit.claim.run_id,
        "pr_number": 9,
        "kind": "execution",
        "source": source,
        "unit": dataclasses.asdict(unit.spec),
        "handoff_id": "ticket-handoff",
        "handoff_state": handoff,
        "native_predecessor": dataclasses.asdict(predecessor),
        "handoff_packet": {"read_only": False},
        "candidate": candidate,
        "continuation_authority": str(store.root),
        "continuation_permit": record["permit"],
        "continuation_consumption": record["consumption"],
    }
    from bmad_loop.campaign_worker_authority import continuation_context

    request["continuation_context"] = continuation_context(saved, saved.units[unit.id])
    observed = []

    def build(spec):
        assert spec.role == "dev" and "ticket 1.1" in spec.prompt and str(plan) in spec.prompt
        assert git(repo, "show", ":owned.py") == "staged partial work"
        assert (repo / "owned.py").read_text() == "unstaged partial work\n"
        assert (repo / "partial-untracked.txt").read_text() == "unfinished residue\n"
        observed.append("retained development")
        (repo / "owned.py").write_text("completed retained work\n")
        (repo / "completed.txt").write_text("verified artifact\n")
        _write_plan(plan, "built", baseline=baseline)
        tree.finds["1.1"] = dataclasses.replace(
            tree.finds["1.1"], row=ticket_row("1.1", status="built")
        )
        tree.next_view = _tree(layout).next_view
        return SessionResult(
            status="completed",
            result_json={"workflow": "auto-dev", "spec_file": str(plan), "status": "built"},
        )

    def review(spec):
        assert spec.role == "review" and (repo / "completed.txt").is_file()
        observed.append("normal review")
        return SessionResult(
            status="completed",
            result_json={"workflow": "auto-dev", "spec_file": str(plan), "status": "built"},
        )

    adapter = MockAdapter([build, review], usage_per_session=TokenUsage(input_tokens=10))
    _marking_tree(tree, plan, monkeypatch)

    def tickets(**kwargs):
        return TicketsEngine(**kwargs, ticket_tree_factory=lambda *_a, **_k: tree)

    composed = compose_continuation(
        request,
        envelope,
        make_adapters=lambda *_a, **_k: {"dev": adapter, "review": adapter, "retro": adapter},
        tickets_engine_cls=tickets,
    )
    original_bytes = (old.run_dir / "state.json").read_bytes()
    composed.engine.run()
    final = load_state(composed.run_dir).tasks["1.1"]
    assert final.phase == Phase.DONE and final.commit_sha
    assert observed == ["retained development", "normal review"]
    assert final.baseline_commit == baseline and final.attempt == 2 and final.review_cycle == 1
    assert final.sessions[0] == task.sessions[0] and final.tokens.input_tokens == 37
    assert verify.is_ancestor(repo, checkpoint, verify.rev_parse_head(repo))
    assert (old.run_dir / "state.json").read_bytes() == original_bytes
    assert (repo / "initial-untracked.txt").read_text() == "owner residue\n"
    verified = [row for row in composed.journal.entries() if row["kind"] == "verify-command-result"]
    assert verified and all(
        row["returncode"] == 0 and row["spawn_error"] is None for row in verified
    )
    assert {"dev", "review"} <= {row["verification_stage"] for row in verified}
    assert store.load(snapshot.id).units[unit.id].bindings["pr"]["number"] == 9
