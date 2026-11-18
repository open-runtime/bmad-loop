"""No-token campaign Git ownership receipts and bounded read-only reconciliation."""

import copy
import dataclasses
import json
import threading
import time
from types import SimpleNamespace

import pytest
from conftest import git

from bmad_loop import campaign_git_activity as activity
from bmad_loop import finalization_recovery as recovery
from bmad_loop.campaign_execution import CampaignExecution
from bmad_loop.campaign_model import CampaignError
from bmad_loop.model import Phase, StoryTask
from bmad_loop.owned_capture import ActorEvidence, CaptureOwnership, CaptureResult
from bmad_loop.policy import Policy


def capture(*, state="dead", census=True, streams=True):
    return CaptureResult(
        None,
        b"",
        b"",
        True,
        False,
        0,
        True,
        CaptureOwnership(
            900001,
            1.0,
            True,
            (ActorEvidence(900001, 1.0, "dead"), ActorEvidence(900002, 2.0, state)),
            1,
            census,
            (),
            streams,
            streams,
            streams,
            True,
            True,
            True,
        ),
    )


class Host:
    def __init__(self, state="dead"):
        self.state = state
        self.calls = []

    def liveness_of(self, pid, identity):
        self.calls.append((pid, identity))
        return self.state


@pytest.fixture
def subject(project, tmp_path):
    repo = project.project
    envelope = CampaignExecution("campaign", "unit", 1, 2, 3, tmp_path / "worker", Policy(), {})
    path, digest = envelope.save()
    request = {
        "campaign_id": "campaign",
        "unit_id": "unit",
        "attempt": 1,
        "generation": 2,
        "launch_id": "launch",
        "run_id": "run",
        "workspace": str(repo),
        "branch": "main",
        "common_dir": str(repo / ".git"),
        "execution": str(path),
        "digest": digest,
        "source": {"kind": "stories", "id": "s", "path": "spec"},
        "source_revision": "source-1",
        "unit": {"id": "unit", "selector": "task", "acceptance": ["unfinished"]},
    }
    bound = activity.binding(request, envelope)
    task = StoryTask("task", 1, phase=Phase.COMMITTING)
    root = recovery._root(repo)
    operation = "a" * 32
    (root / operation).mkdir(parents=True)
    record_path = root / operation / "record.json"
    pointer = root / "active.json"
    pointer.write_text(json.dumps({"schema_version": 1, "id": operation}))
    record = {
        "schema_version": 1,
        "id": operation,
        "repo": str(repo),
        "repo_identity": activity._inode(repo),
        "git_dir": str(repo / ".git"),
        "git_dir_identity": activity._inode(repo / ".git"),
        "branch_ref": "refs/heads/main",
        "state": "uncertain",
        "context": {"campaign_git_activity": activity.context(bound, task)},
        "commands": [],
    }

    def save(value=None):
        record_path.write_text(json.dumps(record if value is None else value))

    def command(value):
        record["commands"] = [
            {
                "command": "commit",
                "known": True,
                "quiescent": value.ownership.quiescent,
                "capture": recovery._capture_document(value),
            }
        ]
        save()

    command(capture())
    return SimpleNamespace(
        repo=repo,
        envelope=envelope,
        request=request,
        bound=bound,
        task=task,
        root=root,
        pointer=pointer,
        path=record_path,
        record=record,
        save=save,
        command=command,
    )


def observe(subject, *, host=None, receipt=None, required=True):
    observer = activity.GitActivityObserver(host=host or Host(), wait_s=1)
    try:
        return observer.observe(subject.repo, subject.bound, receipt, required=required)
    finally:
        observer.close()


def test_lost_ack_stopped_actors_releases_only_activity_not_effect(subject):
    receipt = activity.seal(subject.repo, subject.bound, required=True)
    original = subject.path.read_bytes()
    result = observe(subject, receipt=receipt)
    assert result.state == "quiescent" and result.effect == "unknown"
    assert result.evidence["receipt"] == receipt
    assert subject.path.read_bytes() == original


def test_native_signed_birth_stamps_and_reaped_unstamped_root_are_compatible(subject):
    value = capture()
    owner = dataclasses.replace(
        value.ownership,
        root_identity=-1.0,
        actors=(
            ActorEvidence(900001, None, "dead"),
            ActorEvidence(900001, -1.0, "dead"),
            ActorEvidence(900002, -2.0, "dead"),
        ),
    )
    subject.command(dataclasses.replace(value, ownership=owner))
    assert observe(subject).state == "quiescent"
    owner = dataclasses.replace(
        owner, root_identity=None, census_count=0, actors=(ActorEvidence(900001, None, "dead"),)
    )
    subject.command(dataclasses.replace(value, ownership=owner))
    assert observe(subject).state == "quiescent"


@pytest.mark.parametrize("field", ["source", "source_revision", "unit", "run_id", "attempt"])
def test_missing_launch_task_or_source_is_unknown(subject, field):
    request = copy.deepcopy(subject.request)
    request[field] = True if field == "attempt" else None
    with pytest.raises(CampaignError):
        activity.binding(request, subject.envelope)


def test_complete_prior_operation_can_leave_no_current_activity(subject):
    subject.record.update(state="completed", committed_head="a" * 40)
    subject.record["context"] = None
    subject.save()
    result = observe(subject, required=False)
    assert result.state == "quiescent" and result.effect == "none"
    subject.command(capture(state="unknown"))
    assert observe(subject, required=False).state == "unknown"


def test_host_failure_is_explicit_unknown_and_releases_probe_slot(subject, monkeypatch):
    subject.command(capture(state="unknown"))
    from bmad_loop.process_host import ProcessHostError

    class Broken(Host):
        def liveness_of(self, *args):
            raise ProcessHostError("synthetic opaque failure")

    monkeypatch.setattr(activity, "_PROBE_SLOTS", threading.BoundedSemaphore(1))
    result = observe(subject, host=Broken())
    assert result.state == "unknown" and "ProcessHostError" in result.reason
    assert observe(subject, host=Host()).state == "quiescent"


@pytest.mark.parametrize("state", ["alive", "unknown", "dead"])
def test_complete_recorded_actor_reconciliation_is_fresh_and_non_destructive(subject, state):
    subject.command(capture(state="unknown"))
    receipt = activity.seal(subject.repo, subject.bound, required=True)
    original = subject.path.read_bytes()
    host = Host(state)
    result = observe(subject, host=host, receipt=receipt)
    assert result.state == ("quiescent" if state == "dead" else "waiting")
    assert result.effect == "unknown" and host.calls == [(900002, 2.0)]
    assert subject.path.read_bytes() == original


@pytest.mark.parametrize(
    "fault",
    [
        "census",
        "streams",
        "unstamped",
        "missing-command",
        "pending-command",
        "contradictory-summary",
    ],
)
def test_incomplete_typed_cleanup_cannot_be_upgraded_by_dead_worker(subject, fault):
    subject.command(capture(state="unknown", census=fault != "census", streams=fault != "streams"))
    if fault == "unstamped":
        subject.record["commands"][0]["capture"]["ownership"]["actors"][1]["identity"] = None
    elif fault == "missing-command":
        subject.record["commands"] = []
    elif fault == "pending-command":
        subject.record["commit_observation_pending"] = True
    elif fault == "contradictory-summary":
        subject.record["commands"][0]["quiescent"] = True
    subject.save()
    host = Host("dead")
    result = observe(subject, host=host)
    assert result.state == "unknown" and result.effect == "unknown"
    assert host.calls == []  # No fresh empty PID census is a repair for missing coverage.


@pytest.mark.parametrize(
    "field",
    [
        "launch_id",
        "run_id",
        "task_selector",
        "source_revision",
        "source",
        "unit_digest",
        "config_revision",
        "digest",
    ],
)
def test_foreign_pending_record_cannot_authorize_release(subject, field):
    typed = subject.record["context"]["campaign_git_activity"]["binding"]
    typed[field] = "foreign"
    subject.save()
    result = observe(subject)
    assert result.state == "unknown" and "foreign" in result.reason


def test_missing_typed_context_is_unknown_not_inferred_from_reason(subject):
    subject.record["context"] = None
    subject.record["reason"] = "all writers stopped; approved; finalization finished"
    subject.save()
    assert observe(subject).state == "unknown"


def test_no_record_is_none_only_with_exact_binding_and_without_required_pause(subject):
    subject.pointer.unlink()
    receipt = activity.seal(subject.repo, subject.bound)
    assert observe(subject, receipt=receipt, required=False).effect == "none"
    assert observe(subject, required=True).state == "unknown"
    with pytest.raises(CampaignError, match="typed"):
        activity.seal(subject.repo, {**subject.bound, "source_revision": None})
    incomplete = dict(subject.bound)
    incomplete.pop("launch_id")
    with pytest.raises(CampaignError, match="unknown"):
        activity.seal(subject.repo, incomplete)


def test_explicit_empty_revision_is_retained_without_creating_source_authority(subject):
    request = {**subject.request, "source_revision": ""}
    bound = activity.binding(request, subject.envelope)
    assert bound["source_revision"] == ""
    assert bound["source"] == subject.request["source"]
    # Exact old nonempty revision cannot borrow this new binding.
    result = activity.GitActivityObserver(host=Host(), wait_s=1).observe(subject.repo, bound)
    assert result.state == "unknown"
    subject.record["context"]["campaign_git_activity"] = activity.context(bound, subject.task)
    subject.save()
    receipt = activity.seal(subject.repo, bound, required=True)
    observer = activity.GitActivityObserver(host=Host(), wait_s=1)
    try:
        assert observer.observe(subject.repo, bound, receipt, required=True).state == "quiescent"
    finally:
        observer.close()
    for changed in (
        {**request, "source_revision": None},
        {key: value for key, value in request.items() if key != "source_revision"},
    ):
        with pytest.raises(CampaignError):
            activity.binding(changed, subject.envelope)


@pytest.mark.parametrize(
    "kind", ["stage", "verify", "tracking", "integrate", "queue_verify", "verify_existing"]
)
def test_non_engine_workers_return_separate_unavailable_metadata(subject, kind):
    request = {
        **subject.request,
        "kind": kind,
        "source_revision": "",
        "source": {"kind": "explicit"},
        "unit": {"id": "unit", "selector": ""},
    }
    original = copy.deepcopy(request)
    for _ in range(2):
        assert activity.worker_receipt(request, subject.envelope) == {
            "schema_version": 1,
            "state": "unavailable",
            "applicable": False,
            "code": "not_engine_finalization",
        }
    assert request == original
    # A typed finalization producer is not hidden by a contradictory worker kind.
    assert activity.worker_receipt(request, subject.envelope, required=True)["applicable"] is True


@pytest.mark.parametrize("change", ["source", "task", "run"])
def test_applicable_worker_with_unknown_authority_retains_unavailable_state(subject, change):
    request = {**subject.request, "kind": "execution", "source_revision": ""}
    if change == "source":
        request["source"] = {"kind": "explicit"}
    elif change == "task":
        request["unit"] = {"id": "unit", "selector": ""}
    else:
        request["run_id"] = ""
    unavailable = activity.worker_receipt(request, subject.envelope)
    assert unavailable["state"] == "unavailable" and unavailable["applicable"] is True
    assert "activity" not in unavailable


def test_normal_runtime_first_execution_request_reaches_engine_with_empty_revision(
    project, tmp_path, monkeypatch
):
    from conftest import install_bmad_config
    from test_campaign_workers import envelope as execution_envelope
    from test_stories_engine import SPEC_FOLDER, setup_stories
    from bmad_loop import campaign_adoption, campaign_hold, campaign_workers, runsetup
    from bmad_loop.adapters import multiplexer
    from bmad_loop.adapters.mock import MockAdapter
    from bmad_loop.campaign_model import (
        CampaignSnapshot,
        CampaignSpec,
        CampaignUnit,
        OutboxAction,
        SourceSelector,
        UnitSpec,
        UnitState,
        WorkerClaim,
    )
    from bmad_loop.campaign_runtime import CampaignRuntime
    from bmad_loop.campaign_store import CampaignStore
    from bmad_loop.engine import RunSummary
    from bmad_loop.journal import save_state
    from bmad_loop.stories_engine import StoriesEngine

    repo = project.project
    install_bmad_config(project)
    setup_stories(
        project, [{"id": "first", "title": "First", "description": "Accept first execution"}]
    )
    head = git(repo, "rev-parse", "HEAD")
    context = execution_envelope(repo, tmp_path / "first-worker")
    source = SourceSelector("stories", id="s", path=SPEC_FOLDER)
    spec = UnitSpec(context.unit_id, "s", "epic", "First", selector="first", scopes=("src.txt",))
    claim = WorkerClaim(
        spec.id,
        context.attempt,
        context.generation,
        "first-launch",
        run_id="first-run",
        workspace=str(repo),
        branch="main",
    )
    unit = CampaignUnit(
        spec,
        UnitState.CLAIMED,
        attempt=context.attempt,
        claim=claim,
        bindings={"workspace": str(repo), "branch": "main", "head": head, "base": head},
    )
    snapshot = CampaignSnapshot(
        context.campaign_id,
        "a/b",
        CampaignSpec("First execution", sources=(source,), units=(spec,)),
        {unit.id: unit},
        config_revision=context.config_revision,
        generation=context.generation,
        common_dir=str(repo / ".git"),
    )
    runtime = CampaignRuntime(repo, CampaignStore(tmp_path / "parent"))
    campaign_hold.initialize(runtime.store.directory(snapshot.id), snapshot)
    monkeypatch.setattr(runtime, "_launch_envelope", lambda *args: context)
    monkeypatch.setattr(campaign_adoption, "guard_owned", lambda *args, **kwargs: None)
    monkeypatch.setattr(multiplexer, "configure_multiplexer", lambda *args, **kwargs: None)
    action = OutboxAction(
        claim.launch_id,
        "launch-worker",
        unit.id,
        {"kind": "execution", "checkpoint_base": head, "checkpoint_paths": ["src.txt"]},
        claim.generation,
    )
    prepared = runtime._prepare_launch(snapshot, action)
    assert isinstance(prepared, tuple)
    actual_envelope, request, _launch_env = prepared
    assert "source_revision" not in unit.bindings
    assert request["source_revision"] == ""
    path = campaign_workers.prepare_launch(actual_envelope, request)
    immutable_request = json.loads(path.read_text())
    bound = activity.binding(immutable_request, actual_envelope)
    assert bound["source_revision"] == ""
    assert activity.worker_receipt(immutable_request, actual_envelope)["state"] == "available"
    entered = []
    adapter = MockAdapter([])
    monkeypatch.setattr(
        runsetup,
        "make_adapters",
        lambda *args, **kwargs: {name: adapter for name in ("dev", "review", "retro", "triage")},
    )

    def scripted_engine(engine):
        entered.append(engine.state.run_id)
        engine.state.finished = True
        engine.state.tasks[spec.selector] = StoryTask(spec.selector, 1, phase=Phase.DONE)
        save_state(engine.run_dir, engine.state)
        return RunSummary(engine.state.run_id, 1, 0, 0, False, "", 0, 0)

    monkeypatch.setattr(StoriesEngine, "run", scripted_engine)
    receipt = campaign_workers._execution(immutable_request, actual_envelope)
    assert receipt["outcome"] == "locally_complete" and entered == [claim.run_id]
    assert adapter.sessions == []


@pytest.mark.parametrize("field", ["id", "repo", "repo_identity", "git_dir_identity", "branch_ref"])
def test_foreign_operation_or_workspace_cannot_release_actors(subject, field):
    subject.record[field] = "foreign"
    subject.save()
    assert observe(subject).state == "unknown"


def test_another_worktree_git_directory_cannot_supply_ownership(subject):
    peer = subject.repo / ".git/worktrees/peer"
    peer.mkdir(parents=True)
    (peer / "HEAD").write_text("ref: refs/heads/main\n")
    subject.record.update(git_dir=str(peer), git_dir_identity=activity._inode(peer))
    subject.save()
    result = observe(subject)
    assert result.state == "unknown" and "another worktree" in result.reason


def test_current_branch_move_invalidates_stored_actor_authority(subject):
    (subject.repo / ".git/HEAD").write_text("ref: refs/heads/peer\n")
    result = observe(subject)
    assert result.state == "unknown" and "current branch" in result.reason


def test_sealed_receipt_rejects_changes_to_record_pointer_and_binding(subject):
    receipt = activity.seal(subject.repo, subject.bound, required=True)
    subject.record["reason"] = "changed while paused"
    subject.save()
    assert observe(subject, receipt=receipt).state == "unknown"
    # Re-sealing by the trusted producer is explicit, not acceptance of old bytes.
    current = activity.seal(subject.repo, subject.bound, required=True)
    assert observe(subject, receipt=current).state == "quiescent"
    corrupted = copy.deepcopy(current)
    corrupted["binding"]["run_id"] = "another-run"
    assert observe(subject, receipt=corrupted).state == "unknown"


def test_redirected_or_oversized_record_never_releases(subject, tmp_path):
    copied = tmp_path / "other-record"
    copied.write_bytes(subject.path.read_bytes())
    subject.path.unlink()
    subject.path.symlink_to(copied)
    assert observe(subject).state == "unknown"
    subject.path.unlink()
    subject.path.write_bytes(b"x" * (activity.MAX_RECORD_BYTES + 1))
    assert observe(subject).state == "unknown"


def test_record_change_while_host_query_pending_invalidates_result(subject):
    subject.command(capture(state="unknown"))
    entered, release = threading.Event(), threading.Event()

    class Blocking(Host):
        def liveness_of(self, pid, identity):
            self.calls.append((pid, identity))
            entered.set()
            assert release.wait(2)
            return "dead"

    host = Blocking()
    observer = activity.GitActivityObserver(host=host, wait_s=0.01)
    receipt = activity.seal(subject.repo, subject.bound, required=True)
    try:
        assert (
            observer.observe(subject.repo, subject.bound, receipt, required=True).state == "unknown"
        )
        assert entered.wait(1)
        for _ in range(5):
            assert (
                observer.observe(subject.repo, subject.bound, receipt, required=True).state
                == "unknown"
            )
        assert host.calls == [(900002, 2.0)]
        subject.record["reason"] = "concurrent reconciliation"
        subject.save()
        release.set()
        assert observer._probe is not None and observer._probe.done.wait(1)
        result = observer.observe(subject.repo, subject.bound, receipt, required=True)
        assert result.state == "unknown" and "changed" in result.reason
    finally:
        release.set()
        observer.close()


def test_close_fences_remaining_actor_queries_and_object_replacement_is_globally_bounded(
    subject, monkeypatch
):
    subject.command(capture(state="unknown"))
    entered, release = threading.Event(), threading.Event()

    class Blocking(Host):
        def liveness_of(self, pid, identity):
            self.calls.append((pid, identity))
            entered.set()
            assert release.wait(2)
            return "dead"

    monkeypatch.setattr(activity, "_PROBE_SLOTS", threading.BoundedSemaphore(1))
    host = Blocking()
    first = activity.GitActivityObserver(host=host, wait_s=0.01)
    second = activity.GitActivityObserver(host=host, wait_s=0.01)
    try:
        started = time.monotonic()
        assert first.observe(subject.repo, subject.bound, required=True).state == "unknown"
        assert entered.wait(1)
        first.close()
        for _ in range(10):
            result = second.observe(subject.repo, subject.bound, required=True)
            assert result.state == "unknown" and "capacity" in result.reason
        assert time.monotonic() - started < 0.5
        assert host.calls == [(900002, 2.0)]
        release.set()
        assert first._probe is not None and first._probe.done.wait(1)
        assert first.observe(subject.repo, subject.bound, required=True).state == "unknown"
    finally:
        release.set()
        first.close()
        second.close()


def test_actual_transaction_context_and_record_seal_without_replay(project, tmp_path):
    # Production transaction format, using only the maintained project fixture.
    repo = project.project
    envelope = CampaignExecution("campaign", "unit", 1, 2, 3, tmp_path / "worker", Policy(), {})
    path, digest = envelope.save()
    request = {
        "campaign_id": "campaign",
        "unit_id": "unit",
        "attempt": 1,
        "generation": 2,
        "launch_id": "launch",
        "run_id": "run",
        "workspace": str(repo),
        "branch": "main",
        "common_dir": str(repo / ".git"),
        "execution": str(path),
        "digest": digest,
        "source": {"kind": "stories", "id": "s"},
        "source_revision": "source-1",
        "unit": {"id": "unit", "selector": "task"},
    }
    bound = activity.binding(request, envelope)
    head = git(repo, "rev-parse", "HEAD")
    task = StoryTask("task", 1, phase=Phase.COMMITTING)
    with recovery.begin(
        repo,
        head,
        head,
        "unchanged operation",
        context={"campaign_git_activity": activity.context(bound, task)},
    ) as tx:
        tx.observe_command("commit", capture(state="unknown"))
        tx.uncertain("synthetic unknown effect")
    original = tx.path.read_bytes()
    receipt = activity.seal(repo, bound, required=True)
    observer = activity.GitActivityObserver(host=Host("dead"), wait_s=1)
    try:
        result = observer.observe(repo, bound, receipt, required=True)
    finally:
        observer.close()
    assert result.state == "quiescent" and result.effect == "unknown", result
    assert tx.path.read_bytes() == original


def absence(subject, *, common_dir=None, branch="main"):
    host = Host()
    observer = activity.GitActivityObserver(host=host, wait_s=1)
    try:
        result = observer.observe_absence(
            subject.repo, common_dir=common_dir or subject.repo / ".git", branch=branch
        )
    finally:
        observer.close()
    assert host.calls == []  # This gate cannot reconcile even a recorded pending actor.
    return result


def test_absence_without_record_uses_checkout_authority_not_engine_binding(subject, monkeypatch):
    subject.pointer.unlink()
    monkeypatch.setattr(recovery, "pending", lambda *_: pytest.fail("unbounded pending fallback"))
    result = absence(subject)
    assert result.state == "quiescent" and result.effect == "none"
    proof = result.evidence["absence"]
    assert proof["mode"] == "absence-only" and proof["record"] is None
    assert proof["subject"]["repo_identity"] == activity._inode(subject.repo)
    assert "binding" not in proof and "receipt" not in result.evidence
    # A required typed finalization pause cannot use this weaker absence result.
    assert observe(subject, required=True).state == "unknown"


@pytest.mark.parametrize("state", ["completed", "refused"])
def test_absence_accepts_fully_settled_prior_operation_without_adopting_its_task(subject, state):
    subject.record.update(
        state=state, committed_head="a" * 40, refusal_acknowledged=True, context=None
    )
    subject.save()
    original = subject.path.read_bytes()
    result = absence(subject)
    assert result.state == "quiescent" and result.effect == "none"
    assert result.evidence["absence"]["record"]["id"] == subject.record["id"]
    assert subject.path.read_bytes() == original


@pytest.mark.parametrize("context", ["own", "foreign", "missing", "absence-shaped"])
def test_absence_never_borrows_pending_operation_context(subject, context):
    if context == "foreign":
        subject.record["context"]["campaign_git_activity"]["binding"]["launch_id"] = "foreign"
    elif context == "missing":
        subject.record["context"] = None
    elif context == "absence-shaped":
        # Even a forged context matching the three checkout fields cannot be
        # adopted as an Engine task binding by this gate.
        subject.record["context"]["campaign_git_activity"]["binding"] = {
            "workspace": str(subject.repo),
            "common_dir": str(subject.repo / ".git"),
            "branch": "main",
        }
    subject.save()
    result = absence(subject)
    assert result.state == "unknown" and "Pending Git finalization" in result.reason


@pytest.mark.parametrize("fault", ["census", "streams", "actor", "command", "lease", "foreign"])
def test_absence_rejects_incomplete_or_foreign_settled_operation(subject, fault):
    subject.command(
        capture(
            state="unknown" if fault == "actor" else "dead",
            census=fault != "census",
            streams=fault != "streams",
        )
    )
    subject.record.update(state="completed", committed_head="a" * 40, context=None)
    if fault == "command":
        subject.record["commit_observation_pending"] = True
    elif fault == "lease":
        index = subject.repo / ".git/index"
        subject.record["index_path"] = str(index)
        subject.record["index_lock"] = {"path": str(index) + ".lock", "state": "retained"}
    elif fault == "foreign":
        subject.record["repo_identity"] = [0, 0]
    subject.save()
    assert absence(subject).state == "unknown"


@pytest.mark.parametrize("fault", ["branch", "common", "pointer", "record", "unreadable"])
def test_absence_requires_readable_exact_current_checkout_and_operation(
    subject, monkeypatch, fault
):
    if fault == "record":
        subject.path.unlink()
    elif fault == "unreadable":
        original = activity._read

        def unavailable(path):
            if path == subject.pointer:
                raise PermissionError("synthetic unreadable pointer")
            return original(path)

        monkeypatch.setattr(activity, "_read", unavailable)
    else:
        subject.pointer.unlink()
        if fault == "pointer":
            (subject.repo / ".git/HEAD").write_text("ref: refs/heads/other\n")
    result = absence(
        subject,
        common_dir=subject.repo / "foreign" if fault == "common" else None,
        branch="other" if fault == "branch" else "main",
    )
    assert result.state == "unknown"
