"""Real worker/engine/store terminal flow; only Git actor observations are fake."""

from __future__ import annotations

import dataclasses
import json
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import git, install_bmad_config
from test_campaign_workers import envelope, worker_request
from test_stories_engine import SPEC_FOLDER, setup_stories

from bmad_loop import campaign_git_activity as activity
from bmad_loop import campaign_object_promotion
from bmad_loop import campaign_workers as workers
from bmad_loop import finalization_recovery, policy, runs, runsetup, verify
from bmad_loop.adapters import multiplexer
from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.campaign_model import (
    CampaignError,
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
from bmad_loop.campaign_scheduler import session_usage, writer_available
from bmad_loop.campaign_store import CampaignStore
from bmad_loop.campaign_supervisor import CampaignSupervisor, ObservationStale
from bmad_loop.journal import load_state
from bmad_loop.model import Phase, StoryTask
from bmad_loop.owned_capture import ActorEvidence, CaptureOwnership, CaptureResult
from bmad_loop.stories_engine import StoriesEngine


class FakeHost:
    """Never ask the OS whether these deliberately fictional PIDs exist."""

    def __init__(self):
        self.state = "unknown"
        self.calls = []

    def liveness_of(self, pid, identity):
        assert (pid, identity) == (900002, 124.0)
        self.calls.append((pid, identity, self.state))
        return self.state


def selected(case):
    return case.snapshot.units[case.unit_id]


def action(case):
    return next(row for row in case.snapshot.outbox if row.id == case.action_id)


def observe(case):
    return case.runtime._perform_observe_worker(case.snapshot, action(case))


def harvest(case, observation):
    future = Future()
    future.set_result(observation)
    case.supervisor._active[case.action_id] = future
    case.snapshot = case.supervisor._harvest(case.snapshot)


def assert_reserved(case):
    unit = selected(case)
    assert unit.claim is not None and not unit.claim.quiescent
    assert session_usage(case.snapshot).model == 1
    assert not writer_available(case.snapshot, case.sibling_id)
    assert unit.evidence.get("worker_history", []) == []
    assert unit.evidence.get("dev_attempts_spent", 0) == 0
    assert case.store.load(case.snapshot.id).units[unit.id].claim.quiescent is False


@pytest.fixture
def paused_git_worker(project, tmp_path, monkeypatch):
    runtimes = []

    def build(*, stopped=False, complete=True, missing_receipt=False):
        repo = project.project
        install_bmad_config(project)
        setup_stories(
            project, [{"id": "first", "title": "First", "description": "Keep acceptance"}]
        )
        baseline = git(repo, "rev-parse", "HEAD")
        (repo / "src.txt").write_text("unfinished source contribution\n")
        (repo / "untracked.txt").write_text("preserve untracked contribution\n")
        store = CampaignStore(tmp_path / "parent")
        directory = store.directory("campaign-1") / "workers" / "launch-1"
        context = envelope(
            repo, directory, policy.loads('[gates]\nmode="none"\n[notify]\ndesktop=false\n')
        )
        source = SourceSelector("stories", id="s", path=SPEC_FOLDER)
        spec = UnitSpec(
            context.unit_id,
            "s",
            "epic",
            "Selected",
            selector="first",
            scopes=("src/**",),
            acceptance=("Keep acceptance",),
        )
        sibling = UnitSpec(
            "s:project:other",
            "s",
            "epic",
            "Overlapping",
            selector="other",
            scopes=("src/**",),
            acceptance=("Keep sibling acceptance",),
        )
        claim = WorkerClaim(
            spec.id,
            context.attempt,
            context.generation,
            "launch-1",
            run_id="paused-finalization-run",
            run_dir=str(directory),
            workspace=str(repo),
            branch="main",
            writes=True,
        )
        unit = CampaignUnit(
            spec,
            UnitState.RUNNING,
            attempt=context.attempt,
            claim=claim,
            bindings={"workspace": str(repo), "branch": "main", "head": baseline, "base": baseline},
            evidence={"developer_families": ["anthropic", "openai"]},
        )
        observed_action = OutboxAction(
            "observe-pause",
            "observe-worker",
            spec.id,
            {"launch_id": claim.launch_id},
            context.generation,
            state="sent_unknown",
        )
        snapshot = store.create(
            CampaignSnapshot(
                context.campaign_id,
                "a/b",
                CampaignSpec("Git ownership", sources=(source,), units=(spec, sibling)),
                {unit.id: unit, sibling.id: CampaignUnit(sibling, UnitState.READY)},
                config_revision=context.config_revision,
                generation=context.generation,
                common_dir=str(repo / ".git"),
                outbox=[observed_action],
            )
        )
        adapter = MockAdapter([])
        monkeypatch.setattr(
            runsetup,
            "make_adapters",
            lambda *a, **kw: {role: adapter for role in ("dev", "review", "retro", "triage")},
        )
        monkeypatch.setattr(multiplexer, "configure_multiplexer", lambda *a, **kw: None)
        path = workers.prepare_launch(
            context,
            worker_request(
                repo,
                context,
                kind="execution",
                source=dataclasses.asdict(source),
                source_revision="",
                unit=dataclasses.asdict(spec),
                run_id=claim.run_id,
            ),
        )
        request = json.loads(path.read_text())
        capture = CaptureResult(
            None,
            b"",
            b"",
            True,
            False,
            0,
            True,
            CaptureOwnership(
                900001,
                123.0,
                True,
                (
                    ActorEvidence(900001, 123.0, "dead"),
                    ActorEvidence(900002, 124.0, "dead" if stopped else "unknown"),
                ),
                1,
                complete,
                (),
                True,
                True,
                True,
                True,
                True,
                True,
            ),
        )
        real_git = verify._run_git
        commits = []

        def interrupted_commit(cmd, root, **kwargs):
            if len(cmd) > 3 and cmd[3] == "commit":
                commits.append(tuple(cmd))
                raise verify.GitTimeoutError("scripted lost acknowledgement", capture=capture)
            return real_git(cmd, root, **kwargs)

        monkeypatch.setattr(verify, "_run_git", interrupted_commit)

        def committing_loop(engine):
            task = StoryTask(
                spec.selector, 1, phase=Phase.COMMITTING, baseline_commit=baseline, attempt=2
            )
            engine.state.tasks[spec.selector] = task
            engine._save()
            # Actual Engine finalization supplies its private bound context.
            engine._finalize_commit_phase(task)
            pytest.fail("uncertain finalization did not pause")

        monkeypatch.setattr(StoriesEngine, "_loop", committing_loop)
        assert workers.child_main(path) == 1
        receipt = json.loads((directory / "receipt.json").read_text())
        assert receipt["outcome"] == "paused", receipt
        assert receipt["child_pause"]["cause"]["producer"] == "engine.finalization"
        assert receipt["git_activity"]["state"] == "available", receipt["git_activity"]
        assert len(commits) == 1 and adapter.sessions == []
        state = load_state(Path(receipt["run_dir"]))
        assert state.tasks[spec.selector].phase == Phase.COMMITTING
        record = finalization_recovery.pending(repo)
        assert record is not None and record["state"] == "uncertain"
        assert record["context"]["campaign_git_activity"]["binding"] == activity.binding(
            request, context
        )
        record_path = finalization_recovery._root(repo) / record["id"] / "record.json"
        original_record = record_path.read_bytes()
        if missing_receipt:
            (directory / "receipt.json").unlink()
        monkeypatch.setattr(runs, "read_named_pid_identity", lambda _: (800001, 200.0))
        monkeypatch.setattr(runs, "probe_liveness", lambda *a: "dead")
        mux_calls = []

        class EmptyMux:
            def list_window_ids(self, session):
                mux_calls.append(session)
                return []

        monkeypatch.setattr(multiplexer, "get_multiplexer", lambda: EmptyMux())
        runtime = CampaignRuntime(repo, store, clock=lambda: 100.0)
        monkeypatch.setattr(runtime, "template", lambda _: context)
        host = FakeHost()
        runtime._git_activity_observer = activity.GitActivityObserver(host=host, wait_s=1)
        runtimes.append(runtime)
        return SimpleNamespace(
            repo=repo,
            store=store,
            context=context,
            request=request,
            receipt=receipt,
            snapshot=snapshot,
            unit_id=unit.id,
            sibling_id=sibling.id,
            action_id=observed_action.id,
            runtime=runtime,
            host=host,
            commits=commits,
            record_path=record_path,
            original_record=original_record,
            mux_calls=mux_calls,
            supervisor=CampaignSupervisor(store, runtime, clock=lambda: 100.0),
        )

    yield build
    for runtime in runtimes:
        runtime.close_observers()


def test_unknown_git_actor_retains_reservation_then_fresh_death_releases_once(paused_git_worker):
    case = paused_git_worker()
    for _ in range(2):
        waiting = observe(case)
        assert waiting.state == "waiting", waiting
        harvest(case, waiting)
        assert_reserved(case)
    assert case.host.calls and all(row[2] == "unknown" for row in case.host.calls)
    case.host.state = "dead"
    finished = observe(case)
    assert finished.state == "confirmed", finished
    harvest(case, finished)
    unit = selected(case)
    assert unit.claim.quiescent and unit.hold.kind == "pause_unknown"
    assert session_usage(case.snapshot).model == 0 and writer_available(
        case.snapshot, case.sibling_id
    )
    assert unit.evidence["dev_attempts_spent"] == 2
    assert len(unit.evidence["worker_history"]) == 1
    assert unit.evidence["child_pause"] == case.receipt["child_pause"]
    assert unit.evidence["developer_families"] == ["anthropic", "openai"]
    assert unit.spec.acceptance == ("Keep acceptance",)
    assert unit.evidence["git_activity"]["observation"]["effect"] == "unknown"
    before = json.dumps(dataclasses.asdict(unit), sort_keys=True)
    case.snapshot = case.supervisor._harvest(case.snapshot)
    assert json.dumps(dataclasses.asdict(selected(case)), sort_keys=True) == before
    assert case.record_path.read_bytes() == case.original_record and len(case.commits) == 1
    assert (case.repo / "untracked.txt").read_text() == "preserve untracked contribution\n"


def test_all_stopped_lost_ack_releases_only_capacity(paused_git_worker):
    case = paused_git_worker(stopped=True)
    finished = observe(case)
    assert finished.state == "confirmed", finished
    harvest(case, finished)
    assert selected(case).claim.quiescent and selected(case).hold.kind == "pause_unknown"
    assert case.host.calls == []
    assert case.record_path.read_bytes() == case.original_record
    assert session_usage(case.snapshot).model == 0


def test_incomplete_original_census_is_not_healed_by_empty_worker_or_dead_actor(paused_git_worker):
    case = paused_git_worker(complete=False)
    case.host.state = "dead"
    waiting = observe(case)
    assert waiting.state == "waiting", waiting
    harvest(case, waiting)
    assert_reserved(case)
    assert case.host.calls == []
    assert case.record_path.read_bytes() == case.original_record


def test_interrupted_worker_without_receipt_retains_unknown_effect_hold(paused_git_worker):
    case = paused_git_worker(missing_receipt=True)
    waiting = observe(case)
    assert waiting.state == "waiting" and waiting.data["state"] == "interrupted"
    harvest(case, waiting)
    assert_reserved(case)
    case.host.state = "dead"
    finished = observe(case)
    assert finished.state == "confirmed" and finished.data["state"] == "interrupted"
    harvest(case, finished)
    assert selected(case).claim.quiescent and selected(case).hold.kind == "pause_unknown"
    assert selected(case).hold.evidence["git_activity"]["observation"]["effect"] == "unknown"
    assert case.record_path.read_bytes() == case.original_record


@pytest.mark.parametrize("missing_receipt", [False, True])
def test_promotion_failure_cannot_skip_unsettled_git_gate(
    paused_git_worker, monkeypatch, missing_receipt
):
    case = paused_git_worker(missing_receipt=missing_receipt)
    promoted = []

    def refuse(*args, **kwargs):
        promoted.append(True)
        raise CampaignError("scripted promotion unavailable")

    monkeypatch.setattr(campaign_object_promotion, "promote", refuse)
    waiting = observe(case)
    assert waiting.state == "waiting" and promoted == []
    harvest(case, waiting)
    assert_reserved(case)
    case.host.state = "dead"
    finished = observe(case)
    assert finished.state == "confirmed" and finished.data["object_promotion_error"]
    assert promoted == [True]
    harvest(case, finished)
    assert selected(case).claim.quiescent and selected(case).hold is not None
    assert selected(case).evidence["git_activity"]["observation"]["effect"] == "unknown"
    assert case.record_path.read_bytes() == case.original_record


@pytest.mark.parametrize("missing_receipt", [False, True])
def test_record_change_between_observe_and_apply_keeps_claim_and_retries_observation(
    paused_git_worker, missing_receipt
):
    case = paused_git_worker(stopped=True, missing_receipt=missing_receipt)
    finished = observe(case)
    assert finished.state == "confirmed", finished
    record = json.loads(case.record_path.read_text())
    record["reason"] = "concurrent operator reconciliation changed the record"
    case.record_path.write_text(json.dumps(record))
    with pytest.raises(ObservationStale):
        case.runtime._apply_worker(case.snapshot, selected(case), action(case), finished.data)
    assert_reserved(case)
    harvest(case, finished)
    assert_reserved(case)
    assert action(case).state == "sent_unknown" and action(case).next_retry_at == 101.0
    assert "Git actor evidence changed" in action(case).error
    assert len(case.commits) == 1
