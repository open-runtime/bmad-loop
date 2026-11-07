"""Takeover keeps live actors intact and requires fresh release evidence."""

import dataclasses
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import git
from test_generic_tmux import (
    _dev_handle,
    _dev_spec,
    _ScriptedWatcher,
    _stop_event,
    make_dev_adapter,
)

from bmad_loop import campaign_hold, verify
from bmad_loop.adapters.multiplexer import MultiplexerError
from bmad_loop.campaign_github import MutationIntent
from bmad_loop.campaign_model import (
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    Hold,
    OutboxAction,
    UnitSpec,
    UnitState,
    WorkerClaim,
)
from bmad_loop.campaign_runtime import CampaignRuntime
from bmad_loop.campaign_source_publication import SourceBinding
from bmad_loop.campaign_store import CampaignStore


def configured(project, tmp_path):
    spec = UnitSpec("s:epic:one", "s", "epic", "One", scopes=("src/one",))
    snapshot = CampaignSnapshot("campaign", "org/repo", CampaignSpec("takeover", units=(spec,)))
    unit = CampaignUnit(spec, UnitState.READY_TO_PUBLISH)
    unit.bindings.update(
        workspace=str(project.project),
        branch=git(project.project, "branch", "--show-current").strip(),
        head=verify.rev_parse_head(project.project),
        ready_for_review=True,
    )
    snapshot.units[unit.id] = unit
    snapshot.common_dir = str(project.project / ".git")
    snapshot.bindings.update(
        inventory={"complete": True, "observed_at": 100},
        capacity={"observed_at": 100},
        intake={"observed_at": 100},
    )
    runtime = CampaignRuntime(
        project.project, CampaignStore(tmp_path / "campaigns"), clock=lambda: 100
    )
    runtime._role = lambda *args, **kwargs: None
    return runtime, snapshot, unit


def test_hold_channel_is_scoped_and_unreadable_channel_fails_held(tmp_path):
    path = campaign_hold.channel(tmp_path, "s:one")
    assert not campaign_hold.active(path, campaign="campaign", unit="s:one")
    campaign_hold.write(path, campaign="campaign", unit="s:one", hold="hold-1")
    assert campaign_hold.active(path, campaign="campaign", unit="s:one")
    campaign_hold.write(path, campaign="foreign", unit="s:one", hold="")
    assert campaign_hold.active(path, campaign="campaign", unit="s:one")
    path.write_text("torn")
    assert campaign_hold.active(path, campaign="campaign", unit="s:one")


def test_takeover_suppresses_hcom_keystrokes_and_new_sessions(tmp_path):
    adapter, _ = make_dev_adapter(tmp_path)
    adapter.mux.send_text = lambda *args: pytest.fail("typed into held actor")
    adapter._send_via_hcom = lambda *args: pytest.fail("sent hcom to held actor")
    token = campaign_hold.install(lambda: True)
    try:
        adapter.send_text(_dev_handle(), "continue")
        with pytest.raises(MultiplexerError, match="operator hold"):
            adapter.start_session(_dev_spec(tmp_path))
    finally:
        campaign_hold.reset(token)


def test_held_resultless_stop_waits_for_real_completion_without_nudge_or_timeout(
    tmp_path, monkeypatch
):
    adapter, impl = make_dev_adapter(tmp_path)
    adapter.mux.send_text = lambda *args: pytest.fail("nudged held actor")
    monkeypatch.setattr("bmad_loop.adapters.generic.RESULT_GRACE_S", 0)

    def finish(call):
        if call == 2:
            (impl / "spec-3-1-held.md").write_text(
                "---\nstatus: done\n---\n\n## Auto Run Result\n\nStatus: done\n"
            )

    adapter.watcher = _ScriptedWatcher(
        [_stop_event("3-1-dev-1", "native", None)] * 2, on_call=finish
    )
    token = campaign_hold.install(lambda: True)
    try:
        result = adapter.wait_for_completion(
            _dev_handle(), dataclasses.replace(_dev_spec(tmp_path), timeout_s=0)
        )
    finally:
        campaign_hold.reset(token)
    assert result.status == "completed" and adapter.watcher.calls == 2


def test_live_hold_suppresses_checkpoint_but_preserves_observation(project, tmp_path):
    runtime, snapshot, unit = configured(project, tmp_path)
    unit.claim = WorkerClaim(
        unit.id, 1, 1, "launch", run_id="run", workspace=unit.bindings["workspace"]
    )
    snapshot.outbox.append(OutboxAction("launch", "launch", unit.id, {}, 1, state="confirmed"))
    runtime.control(snapshot, {"kind": "hold", "unit": unit.id, "id": "owner", "reason": "inspect"})
    assert [p.kind for p in runtime.observe_workers(snapshot)] == ["observe-worker"]
    assert campaign_hold.active(
        campaign_hold.channel(runtime.store.directory(snapshot.id), unit.id),
        campaign=snapshot.id,
        unit=unit.id,
    )


def test_release_is_observed_before_clear_and_invalidates_owner_modified_head(project, tmp_path):
    runtime, snapshot, unit = configured(project, tmp_path)
    unit.evidence.update(
        review={"approved": True}, verification={"outcome": "verified"}, integrated={"old": True}
    )
    runtime.control(snapshot, {"kind": "hold", "unit": unit.id, "id": "owner"})
    (project.project / "owner.txt").write_text("owner correction")
    git(project.project, "add", "owner.txt")
    git(project.project, "commit", "-m", "owner correction")
    runtime.control(snapshot, {"kind": "release", "unit": unit.id, "id": "release"})
    assert unit.hold.kind == "operator" and "review" in unit.evidence
    proposal = next(p for p in runtime.proposals(snapshot) if p.kind == "release-observation")
    action = OutboxAction(
        "observe-release", proposal.kind, unit.id, proposal.payload, snapshot.generation
    )
    result = runtime._perform_release_observation(snapshot, action)
    runtime.apply(snapshot, action, result)
    assert unit.hold is None and unit.state == UnitState.SYNCING
    assert "review" not in unit.evidence and "verification" not in unit.evidence
    assert unit.bindings["head"] == verify.rev_parse_head(project.project)
    assert unit.evidence["operator_release_history"][-1]["invalidated"]["review"]


def test_release_does_not_trust_cached_quiescent_process_or_clear_owner_hold(
    project, tmp_path, monkeypatch
):
    runtime, snapshot, unit = configured(project, tmp_path)
    unit.claim = WorkerClaim(
        unit.id, 1, 1, "launch", quiescent=True, run_dir=str(tmp_path / "worker")
    )
    runtime.control(snapshot, {"kind": "hold", "unit": unit.id, "id": "owner"})
    runtime.control(snapshot, {"kind": "release", "unit": unit.id, "id": "release"})
    monkeypatch.setattr("bmad_loop.runs.read_named_pid_identity", lambda path: (123, 456))
    monkeypatch.setattr("bmad_loop.runs.probe_liveness", lambda *args: "alive")
    action = OutboxAction(
        "observe-release",
        "release-observation",
        unit.id,
        unit.bindings["operator_release"],
        snapshot.generation,
    )
    result = runtime._perform_release_observation(snapshot, action)
    assert "exact worker process" in result.data["release_refused"]
    runtime.apply(snapshot, action, result)
    assert unit.hold.id == "owner"
    assert campaign_hold.active(
        campaign_hold.channel(runtime.store.directory(snapshot.id), unit.id),
        campaign=snapshot.id,
        unit=unit.id,
    )


def test_red_ci_prepare_action_does_not_block_green_candidate(project, tmp_path):
    runtime, snapshot, first = configured(project, tmp_path)
    second = CampaignUnit(
        dataclasses.replace(first.spec, id="s:epic:two"),
        UnitState.READY_TO_PUBLISH,
        bindings=dict(first.bindings),
    )
    snapshot.units[second.id] = second
    snapshot.outbox.append(
        OutboxAction(
            "red-ci",
            "prepare-merge",
            first.id,
            {},
            snapshot.generation,
            state="sent_unknown",
            error="required CI pending",
        )
    )
    proposals = runtime.proposals(snapshot)
    assert [(p.kind, p.unit) for p in proposals if p.kind == "prepare-merge"] == [
        ("prepare-merge", second.id)
    ]


def test_release_retains_landed_acceptance_and_pending_failure_hold(project, tmp_path):
    runtime, snapshot, unit = configured(project, tmp_path)
    unit.state = UnitState.MERGED_PENDING_SYNC
    unit.evidence.update(
        integrated={"head": unit.bindings["head"]},
        review={"approved": True},
        pending_failure_hold=dataclasses.asdict(Hold("tracking", "source publication pending")),
    )
    runtime.control(snapshot, {"kind": "hold", "unit": unit.id, "id": "owner"})
    runtime.control(snapshot, {"kind": "release", "unit": unit.id, "id": "release"})
    action = OutboxAction(
        "observe-release",
        "release-observation",
        unit.id,
        unit.bindings["operator_release"],
        snapshot.generation,
    )
    runtime.apply(snapshot, action, runtime._perform_release_observation(snapshot, action))
    assert unit.state == UnitState.MERGED_PENDING_SYNC and unit.hold.kind == "tracking"
    assert unit.evidence["review"]["approved"] and unit.evidence["integrated"]["head"]


def test_takeover_release_never_waives_a_preexisting_owner_approval_hold(project, tmp_path):
    runtime, snapshot, unit = configured(project, tmp_path)
    unit.hold = Hold("owner", "acceptance decision required", id="decision-1")
    runtime.control(snapshot, {"kind": "hold", "unit": unit.id, "id": "takeover"})
    runtime.control(snapshot, {"kind": "hold", "unit": unit.id, "id": "repeat-takeover"})
    runtime._set_hold(unit, Hold("technical_failure", "actor failed while held", id="failure-1"))
    runtime.control(snapshot, {"kind": "release", "unit": unit.id, "id": "release"})
    action = OutboxAction(
        "release-observation",
        "release-observation",
        unit.id,
        unit.bindings["operator_release"],
        snapshot.generation,
    )
    runtime.apply(snapshot, action, runtime._perform_release_observation(snapshot, action))
    assert unit.hold.kind == "owner" and unit.hold.id == "decision-1"
    assert unit.evidence["pending_failure_hold"]["id"] == "failure-1"


def test_release_refuses_changed_canonical_source(project, tmp_path, monkeypatch):
    runtime, snapshot, unit = configured(project, tmp_path)
    binding = SourceBinding(
        "stories",
        unit.id,
        "one",
        str(project.project),
        str(project.project),
        str(project.project / "one.md"),
        "shared-ignored",
        "old",
        "ready",
        None,
    )
    unit.bindings["canonical_source"] = binding.to_dict()
    runtime.source = lambda *args: None
    monkeypatch.setattr(
        "bmad_loop.campaign_source_publication.SourcePublication.capture",
        lambda *args: dataclasses.replace(binding, revision="owner-change"),
    )
    runtime.control(snapshot, {"kind": "hold", "unit": unit.id, "id": "owner"})
    runtime.control(snapshot, {"kind": "release", "unit": unit.id, "id": "release"})
    action = OutboxAction(
        "observe-release",
        "release-observation",
        unit.id,
        unit.bindings["operator_release"],
        snapshot.generation,
    )
    result = runtime._perform_release_observation(snapshot, action)
    assert "source changed" in result.data["release_refused"]
    runtime.apply(snapshot, action, result)
    assert unit.hold.id == "owner"


def test_final_submission_lock_is_short_and_serializes_only_submissions(project, tmp_path):
    runtime, snapshot, unit = configured(project, tmp_path)
    entered, release = threading.Event(), threading.Event()
    second_started, second_entered = threading.Event(), threading.Event()
    order = []

    def submit(snapshot, action):
        order.append(action.id)
        if action.id == "first":
            entered.set()
            assert release.wait(5)
        else:
            second_entered.set()
        return action.id

    runtime._perform_github_submission = submit
    intent = MutationIntent("merge", "merge", "PUT", "/merge", {"sha": "H"}).to_dict()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            runtime._perform_github,
            snapshot,
            OutboxAction("first", "github", unit.id, {"intent": intent}, 1),
        )
        assert entered.wait(5)

        def attempt_second():
            second_started.set()
            return runtime._perform_github(
                snapshot, OutboxAction("second", "github", unit.id, {"intent": intent}, 1)
            )

        second = pool.submit(attempt_second)
        assert second_started.wait(5)
        assert not second_entered.wait(0.05)
        assert order == ["first"]
        release.set()
        assert first.result(5) == "first" and second.result(5) == "second"


def test_issued_direct_merge_fence_ends_only_after_canonical_reconciliation(project, tmp_path):
    runtime, snapshot, first = configured(project, tmp_path)
    first.state = UnitState.MERGE_REQUESTED
    second = CampaignUnit(
        dataclasses.replace(first.spec, id="s:epic:two"),
        UnitState.READY_TO_PUBLISH,
        bindings=dict(first.bindings),
    )
    snapshot.units[second.id] = second
    runtime.store.create(snapshot)
    intent = MutationIntent("second-merge", "merge", "PUT", "/merge", {"sha": "H"}).to_dict()
    result = runtime._perform_github_submission(
        snapshot, OutboxAction("second-merge", "github", second.id, {"intent": intent}, 1)
    )
    assert result.state == "waiting" and "issued direct merge" in result.reason


def test_http_transport_hold_suppresses_prompt_and_session_creation(tmp_path):
    from types import SimpleNamespace

    from test_opencode_http import make_adapter

    from bmad_loop.adapters.base import SessionHandle
    from bmad_loop.adapters.opencode_http import OpencodeServerError

    adapter = make_adapter(tmp_path)
    adapter._sessions["held"] = SimpleNamespace()
    adapter._prompt = lambda *args: pytest.fail("HTTP prompt sent during takeover")
    token = campaign_hold.install(lambda: True)
    try:
        adapter.send_text(SessionHandle("held", "native"), "continue")
        with pytest.raises(OpencodeServerError, match="operator hold"):
            adapter.start_session(_dev_spec(tmp_path))
    finally:
        adapter._sessions.clear()
        campaign_hold.reset(token)


def test_http_held_resultless_idle_waits_for_real_completion_and_ignores_timeout(tmp_path):
    from test_opencode_http import _timeout_driven_session, make_adapter

    from bmad_loop.adapters.base import SessionHandle, SessionSpec

    adapter = make_adapter(tmp_path)
    session = _timeout_driven_session(adapter, lambda: None)
    turns = []

    class IdleTurns:
        def get(self, timeout=None):
            turns.append("idle")
            assert len(turns) <= 2
            return "idle"

    session.events = IdleTurns()
    adapter._abort = lambda *args: pytest.fail("held HTTP session was aborted")
    adapter._result_json = lambda *args, **kwargs: {"status": "done"} if len(turns) == 2 else None
    adapter.send_text = lambda *args: pytest.fail("held HTTP session was nudged")
    token = campaign_hold.install(lambda: True)
    try:
        result = adapter.wait_for_completion(
            SessionHandle("t-1", "native"), SessionSpec("t-1", "dev", "test", tmp_path, timeout_s=0)
        )
    finally:
        campaign_hold.reset(token)
    assert result.status == "completed" and len(turns) == 2


def test_satisfied_decision_promotes_pending_failure_without_waiving_other_gates():
    from test_campaign_decisions import artifact, context, snapshot

    from bmad_loop.campaign_decisions import apply_decision, validate_decision

    ctx = context()
    state = snapshot(ctx)
    unit = state.units["s:e:u"]
    CampaignRuntime._set_hold(unit, Hold("verification_failed", "failed suite", id="verify"))
    CampaignRuntime._set_hold(unit, Hold("required_review", "required remote review", id="review"))
    apply_decision(state, unit.id, validate_decision(artifact(ctx), ctx))
    assert campaign_hold.promote_pending(unit)
    assert unit.hold.id == "verify"
    unit.hold = None  # independently satisfied verification gate
    assert campaign_hold.promote_pending(unit) and unit.hold.id == "review"


def test_repeated_technical_fault_is_coalesced_but_protected_holds_are_retained(project, tmp_path):
    _, _, unit = configured(project, tmp_path)
    unit.hold = Hold("operator", "inspect", id="owner")
    CampaignRuntime._set_hold(unit, Hold("technical_failure", "first", id="first"))
    CampaignRuntime._set_hold(unit, Hold("technical_failure", "latest", id="latest"))
    assert unit.evidence["pending_failure_hold"]["id"] == "latest"
    CampaignRuntime._set_hold(unit, Hold("owner_stop", "stop one", id="one"))
    CampaignRuntime._set_hold(unit, Hold("owner_stop", "stop two", id="two"))
    assert {row["id"] for row in unit.evidence["pending_failure_holds"]} == {"one", "two"}
