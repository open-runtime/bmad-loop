"""Resolve can restart an in-place deferred timeout without losing partial work."""

from __future__ import annotations

import copy
import dataclasses
import json
from types import SimpleNamespace

import pytest
from conftest import (
    dev_effect,
    git,
    install_bmad_config,
    spec_path,
    write_ledger,
    write_spec,
    write_sprint,
)
from test_engine import _committing_dev, _reverify_policy, make_engine, resume_engine

from bmad_loop import cli, resolve, runs, verify
from bmad_loop.adapters.base import SessionResult
from bmad_loop.journal import load_state, save_state
from bmad_loop.model import PAUSE_ESCALATION, Phase, RunState, SessionRecord, StoryTask
from bmad_loop.platform_util import session_task_id
from bmad_loop.policy import AdapterPolicy, RecoveryPolicy, ReviewPolicy

KEY = "1-1-a"


def _deferred_attempt(project, tmp_path, *, latest="timeout"):
    install_bmad_config(project)
    write_sprint(project, {KEY: "ready-for-dev"})
    write_ledger(project, {"DW-1": "open"})
    marker = tmp_path / "verification-ready"
    policy = dataclasses.replace(_reverify_policy(marker), review=ReviewPolicy(enabled=False))
    unbound_retry = latest == "unbound-retry"
    if unbound_retry:
        policy = dataclasses.replace(
            policy,
            adapter=AdapterPolicy(name="codex"),
            recovery=RecoveryPolicy(max_session_resumes=2),
        )
        write_spec(
            spec_path(project, KEY), "in-progress", git(project.project, "rev-parse", "HEAD")
        )

    def timeout_with_work(_spec):
        partial = project.project / "partial.txt"
        partial.write_text("committed partial work\n", encoding="utf-8")
        git(project.project, "add", "partial.txt")
        git(project.project, "commit", "-q", "-m", "kept partial attempt")
        with (project.project / "src.txt").open("a", encoding="utf-8") as source:
            source.write("unfinished source edit\n")
        with spec_path(project, KEY).open("a", encoding="utf-8") as spec:
            spec.write("\nKeep the partial implementation and finish it.\n")
        transcript = tmp_path / "claude-native.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        return SessionResult(
            status="timeout",
            produced_work=True,
            session_id="timed-out-conversation",
            transcript_path=str(transcript),
            result_json=(
                None
                if unbound_retry
                else {"spec_file": str(spec_path(project, KEY)), "status": "in-progress"}
            ),
        )

    engine, adapter = make_engine(
        project,
        [
            (
                timeout_with_work
                if unbound_retry
                else _committing_dev(project, KEY, "first-attempt.txt")
            ),
            (
                (lambda spec: SessionResult(status="crashed", resumed_from=spec.resume_session_id))
                if unbound_retry
                else (
                    timeout_with_work
                    if latest == "timeout"
                    else _committing_dev(project, KEY, "second-attempt.txt")
                )
            ),
        ],
        policy=policy,
    )
    if unbound_retry:
        adapter.profile = SimpleNamespace(resumable=True)
        engine.state.tasks[KEY] = StoryTask(
            story_key=KEY, epic=1, spec_file=str(spec_path(project, KEY))
        )
    summary = engine.run()
    task = engine.state.tasks[KEY]
    assert summary.paused and task.phase == Phase.DEFERRED
    assert len(adapter.sessions) == 2
    if unbound_retry:
        timeout, crash = task.sessions
        assert timeout.status == "timeout" and timeout.adapter == "codex"
        assert timeout.session_id == "timed-out-conversation" and timeout.result_json is None
        assert crash.status == "crashed" and crash.adapter == "codex"
        assert crash.session_id is None and crash.result_json is None
        assert crash.resumed_from == timeout.session_id
        assert adapter.sessions[1].resume_session_id == timeout.session_id
    else:
        assert task.sessions[-1].status == latest
    decisions = [row for row in engine.journal.entries() if row["kind"] == "dev-decision"]
    assert decisions[-1]["produced_work"] is True
    with project.deferred_work.open("a", encoding="utf-8") as ledger:
        ledger.write("\n### DW-2: kept bookkeeping\n\nstatus: open\n")
    (project.project / "kept-notes.txt").write_text("partial attempt notes\n", encoding="utf-8")
    return engine, marker


def _resolve(project, *flags):
    return cli.main(
        [
            "resolve",
            "--project",
            str(project.project),
            "test-run",
            "--story",
            KEY,
            "--no-interactive",
            "--no-resume",
            *flags,
        ]
    )


def _no_launch(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_resume_paused_run", lambda *a, **k: calls.append("resume"))
    monkeypatch.setattr(cli, "_make_adapters", lambda *a, **k: pytest.fail("built real adapters"))
    monkeypatch.setattr(
        resolve, "run_session", lambda *a, **k: pytest.fail("started resolve agent")
    )
    return calls


@pytest.mark.parametrize("later_workflow", [False, True])
@pytest.mark.parametrize("latest", ["timeout", "unbound-retry"])
def test_resolve_deferred_timeout_keeps_work_and_starts_one_fresh_dev(
    project, tmp_path, monkeypatch, capsys, later_workflow, latest
):
    engine, marker = _deferred_attempt(project, tmp_path, latest=latest)
    if later_workflow:
        state = load_state(engine.run_dir)
        state.tasks[KEY].sessions.append(
            SessionRecord(
                task_id="verification-workflow",
                role="dev",
                status="completed",
                label="checks.verify",
            )
        )
        save_state(engine.run_dir, state)
    before = load_state(engine.run_dir).tasks[KEY]
    history = copy.deepcopy([record.to_dict() for record in before.sessions])
    head = git(project.project, "rev-parse", "HEAD")
    ancestry = git(project.project, "rev-list", "HEAD").splitlines()
    source = (project.project / "src.txt").read_bytes()
    committed_source = git(project.project, "show", "HEAD:src.txt")
    ledger = project.deferred_work.read_bytes()
    calls = _no_launch(monkeypatch)

    rc = _resolve(project)

    assert rc == 0, capsys.readouterr().err
    armed = load_state(engine.run_dir).tasks[KEY]
    assert armed.phase == Phase.PENDING and armed.rearmed
    assert armed.generation == before.generation + 1 and armed.attempt == 0
    assert armed.baseline_commit == head != before.baseline_commit
    assert armed.reverify_from == ""
    assert [record.to_dict() for record in armed.sessions] == history
    assert git(project.project, "rev-parse", "HEAD") == head
    assert (project.project / "src.txt").read_bytes() == source
    assert project.deferred_work.read_bytes() == ledger
    assert verify.read_frontmatter(spec_path(project, KEY))["status"] == "ready-for-dev"
    assert calls == []
    marker.write_text("ready\n", encoding="utf-8")
    observed = []
    finish = dev_effect(project, KEY, followup_review=False)

    def fresh_dev(spec):
        observed.append(spec.task_id)
        assert spec.resume_session_id is None
        assert (project.project / "partial.txt").read_text(
            encoding="utf-8"
        ) == "committed partial work\n"
        assert (project.project / "src.txt").read_text(encoding="utf-8").strip() == committed_source
        assert (project.project / "kept-notes.txt").read_text(
            encoding="utf-8"
        ) == "partial attempt notes\n"
        assert project.deferred_work.read_bytes() == ledger
        assert "Keep the partial implementation" in spec_path(project, KEY).read_text(
            encoding="utf-8"
        )
        assert all(sha in git(project.project, "rev-list", "HEAD").splitlines() for sha in ancestry)
        return finish(spec)

    resumed, adapter = resume_engine(project, engine, [fresh_dev])
    resumed._finish_inflight()

    final = load_state(engine.run_dir).tasks[KEY]
    assert final.phase == Phase.DONE
    assert len(adapter.sessions) == 1 and adapter.sessions[0].role == "dev"
    assert observed and observed[0] not in {record["task_id"] for record in history}
    assert [record.to_dict() for record in final.sessions[: len(history)]] == history
    assert all(sha in git(project.project, "rev-list", "HEAD").splitlines() for sha in ancestry)
    assert committed_source in (project.project / "src.txt").read_text(encoding="utf-8")
    assert project.deferred_work.read_bytes() == ledger


def test_resolve_completed_verify_failure_still_requires_reverify(
    project, tmp_path, monkeypatch, capsys
):
    engine, _marker = _deferred_attempt(project, tmp_path, latest="completed")
    before = (engine.run_dir / "state.json").read_bytes()
    _no_launch(monkeypatch)

    assert _resolve(project) == 1

    assert "--reverify" in capsys.readouterr().err
    assert (engine.run_dir / "state.json").read_bytes() == before
    assert _resolve(project, "--reverify") == 0, capsys.readouterr().err
    armed = load_state(engine.run_dir).tasks[KEY]
    assert armed.phase == Phase.DEV_VERIFY and armed.reverify_from == "deferred"


@pytest.mark.parametrize("latest", ["timeout", "unbound-retry"])
def test_reverify_still_refuses_latest_timeout_partial_work(
    project, tmp_path, monkeypatch, capsys, latest
):
    engine, _marker = _deferred_attempt(project, tmp_path, latest=latest)
    before = (engine.run_dir / "state.json").read_bytes()
    _no_launch(monkeypatch)

    assert _resolve(project, "--reverify") == 1

    error = capsys.readouterr().err
    if latest == "unbound-retry":
        assert "no completed dev session result" in error
    else:
        assert "ended timeout" in error and "partial work" in error
    assert (engine.run_dir / "state.json").read_bytes() == before


@pytest.mark.parametrize("isolation", ["mounted", "policy"])
@pytest.mark.parametrize("latest", ["timeout", "unbound-retry"])
def test_resolve_timeout_refuses_isolated_restart_before_writes(
    project, tmp_path, monkeypatch, isolation, latest
):
    engine, _marker = _deferred_attempt(project, tmp_path, latest=latest)
    if isolation == "mounted":
        state = load_state(engine.run_dir)
        state.tasks[KEY].worktree_path = str(tmp_path / "kept-unit")
        save_state(engine.run_dir, state)
    else:
        (project.project / ".bmad-loop" / "policy.toml").write_text(
            '[scm]\nisolation = "worktree"\n', encoding="utf-8"
        )
    before = (engine.run_dir / "state.json").read_bytes()
    spec = spec_path(project, KEY).read_bytes()
    _no_launch(monkeypatch)

    assert _resolve(project) == 1

    assert (engine.run_dir / "state.json").read_bytes() == before
    assert spec_path(project, KEY).read_bytes() == spec


@pytest.mark.parametrize("latest", ["timeout", "unbound-retry"])
def test_deferred_timeout_baseline_probe_fault_refuses_transaction(
    project, tmp_path, monkeypatch, latest
):
    """A timeout re-drive cannot authorize reset to an unverified old baseline.

    Ablation: retain the old best-effort baseline-probe handler for this lane;
    no RearmError propagates and the DEFERRED state is spent despite the fault.
    """
    engine, _marker = _deferred_attempt(project, tmp_path, latest=latest)
    before = (engine.run_dir / "state.json").read_bytes()
    spec = spec_path(project, KEY).read_bytes()
    head = git(project.project, "rev-parse", "HEAD")

    def fail_head(_repo):
        raise verify.GitError("injected baseline HEAD probe failure")

    monkeypatch.setattr(verify, "rev_parse_head", fail_head)
    with pytest.raises(
        runs.RearmError, match="(?i)(baseline|HEAD).*(fail|probe)|fail.*(baseline|HEAD)"
    ):
        runs.rearm_escalation(
            engine.run_dir,
            KEY,
            isolated_redrive=False,
            resolution_recorded=False,
            project_root=project.project,
        )

    assert (engine.run_dir / "state.json").read_bytes() == before
    assert spec_path(project, KEY).read_bytes() == spec
    assert git(project.project, "rev-parse", "HEAD") == head


def _unbound_retry_state(*, generation=0, key=KEY):
    task = StoryTask(story_key=key, epic=1, phase=Phase.DEFERRED, attempt=2, generation=generation)
    task.sessions = [
        SessionRecord(
            task_id=session_task_id(key, "dev", 1, generation),
            role="dev",
            status="timeout",
            session_id="original-native",
        ),
        SessionRecord(
            task_id=session_task_id(key, "dev", 2, generation),
            role="dev",
            status="crashed",
            resumed_from="original-native",
        ),
    ]
    state = RunState(
        run_id="test-run",
        project="sandbox",
        started_at="now",
        tasks={key: task},
        paused_reason="manual recovery",
        paused_stage=PAUSE_ESCALATION,
        paused_story_key=key,
    )
    return state, task


@pytest.mark.parametrize("generation,key", [(0, KEY), (8, KEY), (8, "k" * 130)])
def test_linked_unbound_retry_admits_only_its_current_chain(generation, key):
    state, task = _unbound_retry_state(generation=generation, key=key)
    assert runs.deferred_timeout_rearmable(state, task, key)


@pytest.mark.parametrize(
    "conflict",
    [
        "bound-crash",
        "crash-result",
        "timeout-result",
        "wrong-link",
        "missing-link",
        "missing-native",
        "prior-not-timeout",
        "latest-not-crashed",
        "stale-latest",
        "stale-previous",
        "old-generation",
        "single-record",
        "wrong-phase",
    ],
)
def test_unbound_retry_refuses_conflicting_or_unrelated_history(conflict):
    """Ablation: removing each corresponding admission guard admits its conflict."""
    state, task = _unbound_retry_state(generation=8)
    assert runs.deferred_timeout_rearmable(state, task, KEY)
    previous, latest = task.sessions
    if conflict == "bound-crash":
        latest.session_id = "another-native"
    elif conflict == "crash-result":
        latest.result_json = {}
    elif conflict == "timeout-result":
        previous.result_json = {}
    elif conflict == "wrong-link":
        latest.resumed_from = "unrelated-native"
    elif conflict == "missing-link":
        latest.resumed_from = ""
    elif conflict == "missing-native":
        previous.session_id = ""
        latest.resumed_from = ""
    elif conflict == "prior-not-timeout":
        previous.status = "crashed"
    elif conflict == "latest-not-crashed":
        latest.status = "stalled"
    elif conflict == "stale-latest":
        latest.task_id = session_task_id(KEY, "dev", 3, task.generation)
    elif conflict == "stale-previous":
        previous.task_id = session_task_id(KEY, "dev", 3, task.generation)
    elif conflict == "old-generation":
        task.generation += 1
    elif conflict == "single-record":
        task.sessions.pop(0)
    else:
        task.phase = Phase.ESCALATED

    assert not runs.deferred_timeout_rearmable(state, task, KEY)


@pytest.mark.parametrize(
    "disqualified", ["finished", "sweep", "wrong-owner", "later-task", "no-dev", "mounted"]
)
@pytest.mark.parametrize("unbound_retry", [False, True])
def test_deferred_timeout_rearm_requires_owned_unfinished_attempt(disqualified, unbound_retry):
    """Ablation: returning True from the admission predicate breaks every row."""
    task = StoryTask(story_key=KEY, epic=1, phase=Phase.DEFERRED)
    task.sessions.append(SessionRecord(task_id="timed-out-dev", role="dev", status="timeout"))
    state = RunState(
        run_id="test-run",
        project="sandbox",
        started_at="now",
        tasks={KEY: task},
        paused_reason="manual recovery",
        paused_stage=PAUSE_ESCALATION,
        paused_story_key=KEY,
    )
    if unbound_retry:
        state, task = _unbound_retry_state()
    assert runs.deferred_timeout_rearmable(state, task, KEY)
    if disqualified == "finished":
        state.finished = True
    elif disqualified == "sweep":
        state.run_type = "sweep"
    elif disqualified == "wrong-owner":
        state.paused_story_key = "1-1-b"
    elif disqualified == "later-task":
        state.tasks["1-1-b"] = StoryTask(story_key="1-1-b", epic=1)
    elif disqualified == "mounted":
        task.worktree_path = "kept-unit"
    else:
        task.sessions.clear()

    assert not runs.deferred_timeout_rearmable(state, task, KEY)


def test_deferred_timeout_direct_rearm_refuses_isolated_mode(project, tmp_path):
    """The direct re-arm API must retain the CLI's in-place-only boundary."""
    engine, _marker = _deferred_attempt(project, tmp_path)
    before = (engine.run_dir / "state.json").read_bytes()
    spec = spec_path(project, KEY).read_bytes()

    with pytest.raises(runs.RearmError, match="in-place"):
        runs.rearm_escalation(
            engine.run_dir,
            KEY,
            isolated_redrive=True,
            resolution_recorded=False,
            project_root=project.project,
        )

    assert (engine.run_dir / "state.json").read_bytes() == before
    assert spec_path(project, KEY).read_bytes() == spec


def test_resolve_rechecks_latest_timeout_under_lock(project, tmp_path, monkeypatch):
    """A completed attempt arriving after entry cannot be spent as a timeout.

    Ablation: keep the entry verdict at the locked check; the spy catches the
    re-arm call before runs' second admission check can mask the stale decision.
    """
    engine, _marker = _deferred_attempt(project, tmp_path)
    spec = spec_path(project, KEY).read_bytes()
    _no_launch(monkeypatch)
    current = []
    probes = []

    def liveness(_run_dir):
        probes.append(True)
        if len(probes) == 1:
            state = load_state(engine.run_dir)
            state.tasks[KEY].sessions[-1].status = "completed"
            save_state(engine.run_dir, state)
            current.append((engine.run_dir / "state.json").read_bytes())
        return "dead"

    monkeypatch.setattr(runs, "engine_liveness", liveness)
    monkeypatch.setattr(
        runs, "rearm_escalation", lambda *a, **k: pytest.fail("spent stale timeout admission")
    )

    assert _resolve(project) == 1

    assert len(probes) >= 2
    assert (engine.run_dir / "state.json").read_bytes() == current[0]
    assert spec_path(project, KEY).read_bytes() == spec


def test_resolve_rechecks_isolation_after_confirmation(project, tmp_path, monkeypatch):
    """The restart cannot discard work under a newly selected worktree policy.

    Ablation: omit the deferred lane's locked policy re-read; the re-arm spy
    catches admission under a policy the operator has already replaced.
    """
    engine, _marker = _deferred_attempt(project, tmp_path)
    before = (engine.run_dir / "state.json").read_bytes()
    spec = spec_path(project, KEY).read_bytes()
    calls = _no_launch(monkeypatch)
    confirms = []

    def confirm(_question):
        confirms.append(True)
        (project.project / ".bmad-loop" / "policy.toml").write_text(
            '[scm]\nisolation = "worktree"\n', encoding="utf-8"
        )
        return True

    monkeypatch.setattr(cli, "_confirm", confirm)
    monkeypatch.setattr(
        runs, "rearm_escalation", lambda *a, **k: pytest.fail("spent stale isolation policy")
    )

    rc = cli.main(
        [
            "resolve",
            "--project",
            str(project.project),
            "test-run",
            "--story",
            KEY,
            "--no-interactive",
        ]
    )

    assert rc == 1 and confirms == [True]
    assert calls == []
    assert (engine.run_dir / "state.json").read_bytes() == before
    assert spec_path(project, KEY).read_bytes() == spec


@pytest.mark.parametrize("phase", [Phase.DEFERRED, Phase.ESCALATED])
def test_resolve_context_matches_deferred_timeout_restore_refusal(project, tmp_path, phase):
    """The resolve agent must not advertise a restoration its re-arm rejects.

    Removing the shared deferred-timeout guard turns restore_supported back on
    for that row; the same in-place fixture remains restorable when escalated.
    """
    engine, _marker = _deferred_attempt(project, tmp_path)
    state = load_state(engine.run_dir)
    task = state.tasks[KEY]
    task.phase = phase
    before = (engine.run_dir / "state.json").read_bytes()
    spec = spec_path(project, KEY).read_bytes()

    path, _withheld, _unreadable = resolve.build_context(
        state,
        engine.run_dir,
        KEY,
        isolation="none",
        project_root=project.project,
        code_root=project.repo_root,
    )

    supported = phase == Phase.ESCALATED
    assert json.loads(path.read_text(encoding="utf-8"))["restore_supported"] is supported
    refusal = runs.validate_restore_latch(state, task, KEY)
    if supported:
        assert refusal is None
    else:
        assert refusal is not None and "timeout" in refusal and "restore" in refusal
    assert (engine.run_dir / "state.json").read_bytes() == before
    assert spec_path(project, KEY).read_bytes() == spec
