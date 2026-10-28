"""The factory overview's read model (factory_view): config, readers, metrics.

Fixture factories are real run dirs written with ``save_state`` plus journal
lines at times relative to a pinned clock, so every window, rate and age below is
exact. The slow sources' commands are never spawned: the ticket tree is a fake
TicketTree and TeamRouter/gh answers come through a patched ``_run_json``."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import ticket_row

from bmad_loop import factory_view as fv
from bmad_loop import tickettree
from bmad_loop.journal import save_state
from bmad_loop.model import Phase, RunState, StoryTask, TicketBinding
from bmad_loop.runs import RUNS_DIR

NOW = 1_800_000_000.0
H = 3600.0


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def binding(ref: str, epic_slug: str, epic_id: str) -> TicketBinding:
    return TicketBinding(
        ref=ref,
        id=ref.rpartition(".")[2],
        epic_slug=epic_slug,
        epic_id=epic_id,
        title=f"Ticket {ref}",
        plan="",
        risk="",
        refined=False,
        plan_checkpoint=False,
        done_checkpoint=False,
        hitl=False,
    )


def task(
    key: str, phase: Phase, *, attempt: int = 1, ticket: TicketBinding | None = None
) -> StoryTask:
    t = StoryTask(story_key=key, epic=0, phase=phase, attempt=attempt)
    t.ticket = ticket
    return t


def make_ws(root: Path, name: str) -> Path:
    ws = root / name
    (ws / ".bmad-loop").mkdir(parents=True)
    return ws


def make_run(
    ws: Path,
    run_id: str,
    events: list[dict],
    *,
    tasks: dict[str, StoryTask] | None = None,
    alive: bool = False,
    finished: bool = False,
    paused_stage: str | None = None,
    paused_story_key: str | None = None,
    raw_lines: tuple[bytes, ...] = (),
) -> Path:
    run_dir = ws / RUNS_DIR / run_id
    run_dir.mkdir(parents=True)
    state = RunState(
        run_id=run_id,
        project=str(ws),
        started_at="2026-10-28T08:00:00",
        tasks=tasks or {},
        finished=finished,
        paused_stage=paused_stage,
        paused_reason="paused for the test" if paused_stage else None,
        paused_story_key=paused_story_key,
        source="tickets",
        initiative="init-a",
        initiative_folder=str(ws / "_bmad-output" / "init-a"),
        ticket_epic_filter=ws.name,
        policy_snapshot={"limits": {"dev_stall_grace_s": 600}},
    )
    save_state(run_dir, state)
    with (run_dir / "journal.jsonl").open("wb") as f:
        for event in events:
            f.write(json.dumps(event).encode("utf-8") + b"\n")
        for raw in raw_lines:
            f.write(raw)
    if alive:
        (run_dir / "engine.pid").write_text(str(os.getpid()), encoding="utf-8")
    return run_dir


def ev(ts: float, kind: str, **fields: object) -> dict:
    return {"ts": ts, "kind": kind, **fields}


def heartbeat(run_dir: Path, task_id: str, *, ts: float, idle: float, nudges: int = 0) -> None:
    d = run_dir / "tasks" / task_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "heartbeat.json").write_text(
        json.dumps(
            {
                "ts": ts,
                "transcript_idle_s": idle,
                "stall_nudges_sent": nudges,
                "quota_waiting": False,
            }
        ),
        encoding="utf-8",
    )


def build_factory(tmp_path: Path, now: float) -> SimpleNamespace:
    """A factory of three epic workspaces, its events timed relative to ``now``.

    alpha: running story 1.3 after 1.1 and 1.2 (plus an older run's 1.0), holding
    the build lock; beta: paused at an escalation, queued for nextest, one torn
    journal line; gamma: held by the scheduler, never run."""
    root = tmp_path / "epic"
    alpha = make_ws(root, "epic-alpha")
    beta = make_ws(root, "epic-beta")
    gamma = make_ws(root, "epic-gamma")
    make_run(
        alpha,
        "20261027-000000-aaaa",
        [
            ev(now - 31 * H, "story-start", story_key="1.0"),
            ev(
                now - 30.5 * H,
                "session-end",
                task_id="1.0-dev-1",
                status="completed",
                tokens_weighted=99,
            ),
            ev(now - 30 * H, "story-done", story_key="1.0", commit="0" * 40),
        ],
        tasks={"1.0": task("1.0", Phase.DONE)},
        finished=True,
    )
    alpha_run = make_run(
        alpha,
        "20261028-000000-bbbb",
        [
            ev(now - 5 * H, "run-start"),
            ev(now - 5 * H, "story-start", story_key="1.1"),
            ev(
                now - 5 * H + 10,
                "session-start",
                task_id="1.1-dev-1",
                story_key="1.1",
                role="dev",
                adapter="claude",
                model="opus",
            ),
            ev(
                now - 4 * H,
                "session-end",
                task_id="1.1-dev-1",
                status="completed",
                tokens_weighted=1000,
            ),
            ev(now - 3.5 * H, "story-done", story_key="1.1", commit="a" * 40),
            ev(now - 3 * H, "story-start", story_key="1.2"),
            ev(
                now - 3 * H + 5,
                "session-start",
                task_id="1.2-dev-1",
                story_key="1.2",
                role="dev",
                adapter="claude",
                model="opus",
            ),
            ev(
                now - 2 * H,
                "session-end",
                task_id="1.2-dev-1",
                status="stalled",
                tokens_weighted=500,
            ),
            ev(now - 2 * H, "run-paused", stage="escalation", reason="manual recovery\nmore"),
            ev(now - 1.5 * H, "run-resume"),
            ev(
                now - 1.5 * H + 5,
                "session-start",
                task_id="1.2-dev-2",
                story_key="1.2",
                role="dev",
                adapter="claude",
                model="opus",
            ),
            ev(
                now - 1 * H,
                "session-end",
                task_id="1.2-dev-2",
                status="completed",
                tokens_weighted=2000,
            ),
            ev(now - 0.5 * H, "story-done", story_key="1.2", commit="b" * 40),
            ev(now - 0.4 * H, "story-start", story_key="1.3"),
            ev(
                now - 0.4 * H + 5,
                "session-start",
                task_id="1.3-dev-1",
                story_key="1.3",
                role="dev",
                adapter="claude",
                model="opus",
            ),
            ev(now - 0.3 * H, "plugin-hook", plugin="p", stage="post_session", rc=0),
        ],
        tasks={
            "1.1": task("1.1", Phase.DONE),
            "1.2": task("1.2", Phase.DONE, attempt=2),
            "1.3": task("1.3", Phase.DEV_RUNNING, ticket=binding("1.3", "epic-alpha", "1")),
        },
        alive=True,
    )
    heartbeat(alpha_run, "1.3-dev-1", ts=now - 30, idle=400)
    beta_run = make_run(
        beta,
        "20261028-010000-cccc",
        [
            ev(now - 2 * H, "story-start", story_key="2.1"),
            ev(
                now - 2 * H + 5,
                "session-start",
                task_id="2.1-dev-1",
                story_key="2.1",
                role="dev",
                adapter="codex",
                model="",
            ),
            ev(
                now - 1 * H,
                "session-end",
                task_id="2.1-dev-1",
                status="timeout",
                tokens_weighted=700,
            ),
            ev(
                now - 1 * H + 10,
                "story-escalated",
                story_key="2.1",
                reason="verify environment fault (rc=75)\ndetail",
            ),
            ev(
                now - 1 * H + 20,
                "run-paused",
                stage="escalation",
                reason="verify environment fault (rc=75)",
            ),
        ],
        tasks={
            "2.1": task("2.1", Phase.ESCALATED, attempt=2, ticket=binding("2.1", "epic-beta", "2"))
        },
        paused_stage="escalation",
        paused_story_key="2.1",
        raw_lines=(b"{this is not json\n",),
    )
    main = tmp_path / "main"
    (main / ".bmad-loop").mkdir(parents=True)
    scheduler_state = main / ".bmad-loop" / "scheduler-state.json"
    scheduler_state.write_text(
        json.dumps(
            {
                "schema": 2,
                "updated_at": iso(now - 300),
                "epics": {
                    "init-a/epic-alpha": {
                        "workspace": str(alpha),
                        "status": "running",
                        "reason": "",
                        "epic": "epic-alpha",
                        "epic_id": 1,
                        "branch": "epic/epic-alpha",
                        "launches": 1,
                        "last_status": {"run_id": "20261028-000000-bbbb"},
                    },
                    "init-a/epic-gamma": {
                        "workspace": str(gamma),
                        "status": "held",
                        "reason": "held until init-a/1.3 is done\nsecond line",
                        "epic": "epic-gamma",
                        "epic_id": 3,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    scheduler_toml = main / "scheduler.toml"
    scheduler_toml.write_text("[scheduler]\nmax_parallel_epics = 4\n", encoding="utf-8")
    locks = tmp_path / "locks"
    (locks / "build.lock").mkdir(parents=True)
    (locks / "build.lock" / "owner.json").write_text(
        json.dumps(
            {
                "name": "build",
                "pid": 4242,
                "cwd": str(alpha / "implementation" / "rem"),
                "command": "cargo build --locked",
                "acquired_at": iso(now - 600),
            }
        ),
        encoding="utf-8",
    )
    (locks / "nextest.queue").mkdir()
    (locks / "nextest.queue" / "00000000000000000001-77-abcd1234.json").write_text(
        json.dumps(
            {"pid": 77, "cwd": str(beta), "enqueued_at": iso(now - 120), "class": "targeted"}
        ),
        encoding="utf-8",
    )
    (locks / "nextest.queue" / ".00000000000000000002-78-x.tmp").write_text("{}", encoding="utf-8")
    config = fv.FactoryConfig(
        name="test-factory",
        source="test",
        workspaces_root=root,
        scheduler_state=scheduler_state,
        scheduler_config=scheduler_toml,
        locks_dir=locks,
        tickets=False,
    )
    return SimpleNamespace(
        tmp=tmp_path,
        root=root,
        alpha=alpha,
        beta=beta,
        gamma=gamma,
        alpha_run=alpha_run,
        beta_run=beta_run,
        scheduler_state=scheduler_state,
        locks=locks,
        config=config,
    )


@pytest.fixture
def factory(tmp_path: Path) -> SimpleNamespace:
    return build_factory(tmp_path, NOW)


def snapshot(config: fv.FactoryConfig) -> fv.FactorySnapshot:
    snap = fv.FactoryReader(config, clock=lambda: NOW).poll()
    assert snap is not None
    return snap


def by_label(snap: fv.FactorySnapshot) -> dict[str, fv.EpicRow]:
    return {row.label: row for row in snap.rows}


# ------------------------------------------------------------------ config


def test_default_config_describes_this_factory():
    config = fv.default_config()
    assert config.name == "runtime_embeddable_memory_cli"
    assert config.workspaces_root == Path(
        "/Volumes/External-Development-Drive/Paradev/worktrees/runtime_embeddable_memory_cli/epic"
    )
    assert config.scheduler_state == Path(
        "/Volumes/External-Development-Drive/Paradev/runtime_embeddable_memory_cli/.bmad-loop/scheduler-state.json"
    )
    assert config.locks_dir == Path.home() / ".local/state/bmad-factory/locks"
    assert dict(config.capacity_models) == {"Opus": "claude-opus-5-5", "Codex": "gpt-6-astra"}
    assert config.prs_repo == "open-runtime/runtime_embeddable_memory_cli"
    assert config.program_initiatives == ("initiative-rem-factory-bootstrap", "initiative-rem")
    assert config.source == fv.DEFAULT_CONFIG_SOURCE


def test_parse_config_resolves_paths_against_the_config_folder(tmp_path: Path):
    config = fv.parse_config(
        '[factory]\nworkspaces_root = "epics"\nscheduler_state = "~/s.json"\nrefresh_s = 5\n',
        source="x",
        base=tmp_path,
    )
    assert config.workspaces_root == tmp_path / "epics"
    assert config.scheduler_state == Path.home() / "s.json"
    assert config.refresh_s == 5.0
    assert config.name == tmp_path.name  # the root's parent folder names the factory
    assert config.locks_dir is None and config.capacity_models == () and config.prs_repo is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[factory]\nname = 'x'\n", "workspaces_root is required"),
        (
            "[factory]\nworkspaces_root = 'w'\nworkspace_root = 'typo'\n",
            "unknown key(s) in [factory]: workspace_root",
        ),
        ("[factory]\nworkspaces_root = 'w'\n[other]\n", "unknown top-level key(s) other"),
        ("[factory]\nworkspaces_root = 'w'\nrefresh_s = 0\n", "refresh_s must be between"),
        (
            "[factory]\nworkspaces_root = 'w'\n[factory.program]\ninitiatives = ['a']\n",
            "initiatives needs a root",
        ),
        (
            "[factory]\nworkspaces_root = 'w'\n[factory.prs]\nrepo = 'nope'\n",
            "repo must be OWNER/NAME",
        ),
        (
            "[factory]\nworkspaces_root = 'w'\n[factory.capacity]\nmodels = ['x']\n",
            "models must map",
        ),
        ("factory = 3\n", "no [factory] table"),
        ("[factory\n", "not valid TOML"),
    ],
)
def test_parse_config_refuses_what_it_cannot_honor(text: str, message: str):
    with pytest.raises(fv.FactoryConfigError, match=None) as caught:
        fv.parse_config(text, source="cfg.toml")
    assert message in str(caught.value)
    assert str(caught.value).startswith("cfg.toml")


def test_resolve_config_prefers_explicit_then_project_file_then_default(tmp_path: Path):
    project = tmp_path / "project"
    (project / ".bmad-loop").mkdir(parents=True)
    assert fv.resolve_config(project).source == fv.DEFAULT_CONFIG_SOURCE
    local = project / fv.FACTORY_CONFIG_REL
    local.write_text("[factory]\nworkspaces_root = 'w'\n", encoding="utf-8")
    assert fv.resolve_config(project).workspaces_root == local.parent / "w"
    explicit = tmp_path / "other.toml"
    explicit.write_text("[factory]\nname = 'other'\nworkspaces_root = '/x'\n", encoding="utf-8")
    assert fv.resolve_config(project, explicit).name == "other"
    with pytest.raises(fv.FactoryConfigError, match="cannot read"):
        fv.resolve_config(project, tmp_path / "missing.toml")
    # a broken project file is an error, never quietly replaced by the default
    local.write_text("[factory]\n", encoding="utf-8")
    with pytest.raises(fv.FactoryConfigError, match="workspaces_root is required"):
        fv.resolve_config(project)


# ---------------------------------------------------------------- snapshot


def test_rows_read_every_workspace_with_its_scheduler_entry(factory):
    snap = snapshot(factory.config)
    assert [row.label for row in snap.rows] == ["alpha", "beta", "gamma"]  # by epic id
    assert [row.epic_id for row in snap.rows] == ["1", "2", "3"]
    assert snap.running == 1
    assert snap.ceiling == 4
    assert snap.roster_updated_at == NOW - 300
    assert dict(snap.roster_counts) == {"running": 1, "held": 1}
    assert {s.name: s.state for s in snap.sources} == {
        "workspaces": "ok",
        "scheduler": "ok",
        "locks": "ok",
        "tickets": "off",
        "program": "off",
        "TeamRouter": "off",
        "gh": "off",
    }


def test_running_row_shows_story_stage_time_quiet_and_lock(factory):
    alpha = by_label(snapshot(factory.config))["alpha"]
    assert alpha.status == "running"
    assert alpha.run_id == "20261028-000000-bbbb"
    assert (alpha.story, alpha.phase, alpha.attempt) == ("1.3", "dev-running", 1)
    # the stage began at the open session's start; a plugin hook does not restart it
    assert alpha.since == NOW - 0.4 * H + 5
    assert alpha.agent == "claude·opus"
    assert alpha.heartbeat_age_s == 30
    assert alpha.quiet_s == 430  # transcript idle at the heartbeat, plus the heartbeat's age
    assert alpha.quiet_warn_s == 300
    assert alpha.lock == "holds build 10m"
    assert alpha.done_total == 3
    assert alpha.done_window == (1, 2, 2, 3)
    assert (alpha.stalls, alpha.parks, alpha.gates) == (1, 1, 0)
    assert alpha.health == "warn"
    assert "transcript quiet 7m" in alpha.notes
    assert alpha.roster is not None and alpha.roster.status == "running"
    assert alpha.faults == ()


def test_paused_row_is_an_alert_and_names_its_queue_place(factory):
    beta = by_label(snapshot(factory.config))["beta"]
    assert (beta.status, beta.paused_stage) == ("paused", "escalation")
    assert (beta.story, beta.phase, beta.attempt) == ("2.1", "escalated", 2)
    assert beta.since == NOW - 1 * H + 20
    assert beta.agent is None and beta.quiet_s is None  # no live session on a paused run
    assert beta.lock == "waits nextest #1 2m"
    assert beta.health == "alert"
    assert beta.notes[0] == "paused at escalation"
    assert beta.faults == ("1 unreadable journal line(s)",)  # the torn line is counted, not dropped


def test_never_run_workspace_shows_the_scheduler_hold(factory):
    gamma = by_label(snapshot(factory.config))["gamma"]
    assert gamma.status == fv.NO_RUN
    assert gamma.health == "idle"
    assert gamma.roster is not None
    assert (gamma.roster.status, gamma.roster.reason) == ("held", "held until init-a/1.3 is done")


def test_throughput_counts_windows_rates_cycle_and_tokens(factory):
    t = snapshot(factory.config).throughput
    assert t.done == (1, 2, 2, 3)
    assert t.rates == pytest.approx((1.0, 2 / 6, 2 / 24))
    assert t.hourly[-1] == 1 and t.hourly[20] == 1 and sum(t.hourly) == 2
    assert t.cycle_count == 2
    assert t.cycle_median_s == pytest.approx((1.5 * H + 2.5 * H) / 2)
    assert t.tokens_6h == 1000 + 500 + 2000 + 700
    assert t.stalls_24h == 2  # alpha's stalled session and beta's timeout
    assert t.parks_24h == 2
    assert t.merged is None


def test_feed_merges_epics_newest_first(factory):
    feed = snapshot(factory.config).feed
    assert [f.ts for f in feed] == sorted((f.ts for f in feed), reverse=True)
    assert (feed[0].epic, feed[0].subject, feed[0].text) == (
        "alpha",
        "1.3-dev-1",
        "session started (claude·opus)",
    )
    texts = {(f.epic, f.text) for f in feed}
    assert ("alpha", "story done bbbbbbbbbb") in texts
    assert ("beta", "escalated: verify environment fault (rc=75)") in texts
    assert all(f.kind != "plugin-hook" for f in feed)


def test_poll_reads_appended_events_and_withholds_a_partial_line(factory):
    reader = fv.FactoryReader(factory.config, clock=lambda: NOW)
    first = reader.poll()
    assert first is not None and by_label(first)["alpha"].done_total == 3
    journal = factory.alpha_run / "journal.jsonl"
    with journal.open("ab") as f:
        f.write(
            json.dumps(ev(NOW - 60, "story-done", story_key="1.3", commit="c" * 40)).encode("utf-8")
            + b"\n"
        )
        f.write(b'{"ts": 1, "kind": "story-start", "story_key": "1.4"')  # no newline yet
    second = reader.poll()
    assert second is not None
    assert by_label(second)["alpha"].done_total == 4
    assert second.throughput.done[0] == 2
    assert second.feed[0].text == "story done cccccccccc"
    assert by_label(second)["alpha"].faults == ()  # the unterminated line is not a fault yet


def test_polling_writes_nothing(factory, monkeypatch):
    def tree() -> dict[str, tuple[int, str]]:
        out: dict[str, tuple[int, str]] = {}
        for path in sorted(factory.tmp.rglob("*")):
            rel = str(path.relative_to(factory.tmp))
            if path.is_file():
                out[rel] = (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
            else:
                out[rel] = (0, "dir")
        return out

    before = tree()
    config = fv.FactoryConfig(**{**factory.config.__dict__, "tickets": True})
    monkeypatch.setattr(tickettree, "TicketTree", _FakeTree)
    _FakeTree.trees = {}
    reader = fv.FactoryReader(config, clock=lambda: NOW)
    for _ in range(3):
        reader.poll()
        reader.poll_slow(force=True)
    assert tree() == before


# --------------------------------------------------------------- degrading


def test_missing_sources_degrade_visibly(tmp_path: Path):
    config = fv.FactoryConfig(
        name="empty",
        source="test",
        workspaces_root=tmp_path / "nowhere",
        scheduler_state=tmp_path / "no-state.json",
        locks_dir=tmp_path / "no-locks",
    )
    snap = snapshot(config)
    assert snap.rows == ()
    states = {s.name: (s.state, s.detail) for s in snap.sources}
    assert states["workspaces"] == ("absent", f"{tmp_path / 'nowhere'} does not exist")
    assert states["scheduler"][0] == "absent"
    assert states["locks"][0] == "absent"
    assert snap.throughput.done == (0, 0, 0, 0) and snap.throughput.cycle_median_s is None


def test_unreadable_scheduler_state_keeps_its_last_good_read(factory):
    reader = fv.FactoryReader(factory.config, clock=lambda: NOW)
    assert reader.poll() is not None
    factory.scheduler_state.write_text("{not json", encoding="utf-8")
    snap = reader.poll()
    assert snap is not None
    state = snap.source("scheduler")
    assert state is not None and state.state == "fault" and "unreadable" in state.detail
    assert by_label(snap)["gamma"].roster is not None  # the last good roster still shows


def test_unreadable_lock_record_is_a_lock_fault(factory):
    (factory.locks / "nextest.lock").mkdir()
    (factory.locks / "nextest.lock" / "owner.json").write_text("{", encoding="utf-8")
    snap = snapshot(factory.config)
    state = snap.source("locks")
    assert state is not None and (state.state, state.detail) == (
        "fault",
        "unreadable: nextest.lock",
    )
    assert [h.name for h in snap.locks] == ["build"]


def test_slot_and_lock_folder_mid_creation(factory):
    (factory.locks / "nextest@2.lock").mkdir()
    (factory.locks / "nextest@2.lock" / "owner.json").write_text(
        json.dumps({"name": "nextest", "cwd": str(factory.beta), "acquired_at": iso(NOW - 60)}),
        encoding="utf-8",
    )
    (factory.locks / "nextest.lock").mkdir()  # no owner.json yet: being created
    snap = snapshot(factory.config)
    assert [(h.name, h.slot) for h in snap.locks] == [("build", 1), ("nextest", 2)]
    assert by_label(snap)["beta"].lock == "holds nextest 1m · waits nextest #1 2m"


@pytest.mark.parametrize(
    ("hb_age", "idle", "health", "note"),
    [
        (30, 10, "ok", None),
        (30, 400, "warn", "transcript quiet 7m"),
        (300, 10, "alert", "heartbeat 5m old: the engine's wait loop is not ticking"),
    ],
)
def test_live_session_health(factory, hb_age, idle, health, note):
    scheduler = json.loads(factory.scheduler_state.read_text(encoding="utf-8"))
    del scheduler["epics"]["init-a/epic-alpha"]
    factory.scheduler_state.write_text(json.dumps(scheduler), encoding="utf-8")
    heartbeat(factory.alpha_run, "1.3-dev-1", ts=NOW - hb_age, idle=idle)
    alpha = by_label(snapshot(factory.config))["alpha"]
    assert alpha.health == health
    assert (note in alpha.notes) if note else alpha.notes == ()


def test_heartbeat_from_before_the_open_session_is_ignored(factory):
    heartbeat(factory.alpha_run, "1.3-dev-1", ts=NOW - 2 * H, idle=5000)
    alpha = by_label(snapshot(factory.config))["alpha"]
    assert alpha.quiet_s is None and alpha.heartbeat_age_s is None


def test_gate_pause_is_a_warning_not_an_alert(tmp_path: Path):
    ws = make_ws(tmp_path / "epic", "epic-delta")
    make_run(
        ws,
        "20261028-020000-dddd",
        [ev(NOW - H, "run-paused", stage="story-checkpoint", reason="checkpoint")],
        tasks={"4.1": task("4.1", Phase.DONE)},
        paused_stage="story-checkpoint",
    )
    snap = snapshot(fv.FactoryConfig(name="t", source="t", workspaces_root=tmp_path / "epic"))
    (row,) = snap.rows
    assert (row.status, row.health, row.notes) == (
        "paused",
        "warn",
        ("waiting at story-checkpoint",),
    )
    assert (row.gates, row.parks) == (1, 0)


# ------------------------------------------------------------ slow sources


class _FakeTree:
    trees: dict[Path, list] = {}
    calls: list[tuple[Path, str | None, Path | None]] = []
    fail: Exception | None = None

    def __init__(self, root, pinned_initiative=None, **_kw):
        self.root = Path(root)
        self.pinned = pinned_initiative

    def status(self, folder=None):
        _FakeTree.calls.append((self.root, self.pinned, folder))
        if _FakeTree.fail is not None:
            raise _FakeTree.fail
        rows = _FakeTree.trees.get(self.root, [])
        return SimpleNamespace(tickets=tuple(rows), epics=(1, 2))


@pytest.fixture
def fake_tree(monkeypatch):
    monkeypatch.setattr(tickettree, "TicketTree", _FakeTree)
    monkeypatch.setattr(fv, "TICKETS_EVERY_S", 0.0)
    _FakeTree.trees, _FakeTree.calls, _FakeTree.fail = {}, [], None
    return _FakeTree


def test_ticket_progress_is_read_per_epic_and_only_when_the_tree_changes(
    factory, fake_tree, monkeypatch
):
    folder = factory.alpha / "_bmad-output" / "init-a"
    folder.mkdir(parents=True)
    plan = folder / "story-1.md"
    plan.write_text("---\nstatus: done\n---\n", encoding="utf-8")
    fake_tree.trees[factory.alpha] = [
        ticket_row("1.1", epic="epic-alpha", status="done"),
        ticket_row("1.2", epic="epic-alpha", status="done"),
        ticket_row("1.3", epic="epic-alpha", status="in-progress"),
        ticket_row("1.4", epic="epic-alpha", status="dropped"),
        ticket_row("1.5", epic="epic-alpha"),
        ticket_row("9.1", epic="epic-other", status="done"),
    ]
    gamma_folder = factory.gamma / "_bmad-output" / "init-a"
    monkeypatch.setattr(fv, "_initiative_folder", lambda root, initiative: (gamma_folder, "init-a"))
    config = fv.FactoryConfig(**{**factory.config.__dict__, "tickets": True})
    reader = fv.FactoryReader(config, clock=lambda: NOW)
    reader.poll_slow()  # before any fast poll: no targets yet, nothing read
    assert fake_tree.calls == []
    reader.poll()
    reader.poll_slow()
    snap = reader.poll()
    assert snap is not None
    alpha = by_label(snap)["alpha"]
    assert alpha.tickets is not None
    assert (alpha.tickets.done, alpha.tickets.total) == (2, 4)  # dropped left out, other epics too
    assert dict(alpha.tickets.by_state) == {"done": 2, "in-progress": 1, "dropped": 1, "planned": 1}
    assert (factory.alpha, "init-a", folder) in fake_tree.calls  # pinned to the run's initiative
    gamma = by_label(snap)["gamma"]
    assert gamma.tickets is not None and gamma.tickets.fault == "no tickets of epic-gamma in init-a"
    calls = len(fake_tree.calls)
    reader.poll_slow()
    assert len(fake_tree.calls) == calls  # nothing changed: every tree answered from its signature
    plan.write_text("---\nstatus: in-progress\n---\n", encoding="utf-8")
    os.utime(plan, ns=(1, 1))
    reader.poll_slow()
    assert [c[0] for c in fake_tree.calls[calls:]] == [factory.alpha]  # only the changed tree


def test_tickets_are_read_on_the_first_slow_poll_after_the_first_fast_one(factory, monkeypatch):
    # TICKETS_EVERY_S keeps its real value here: a slow poll that ran before any
    # fast poll had no targets, and must not push the first read a whole interval out.
    monkeypatch.setattr(tickettree, "TicketTree", _FakeTree)
    _FakeTree.trees = {factory.alpha: [ticket_row("1.1", epic="epic-alpha", status="done")]}
    _FakeTree.calls, _FakeTree.fail = [], None
    folder = factory.gamma / "_bmad-output" / "init-a"
    monkeypatch.setattr(fv, "_initiative_folder", lambda root, initiative: (folder, "init-a"))
    config = fv.FactoryConfig(**{**factory.config.__dict__, "tickets": True})
    reader = fv.FactoryReader(config, clock=lambda: NOW)
    reader.poll_slow()
    snap = reader.poll()
    assert snap is not None
    state = snap.source("tickets")
    assert state is not None and state.state == "pending"
    reader.poll_slow()
    assert {call[0] for call in _FakeTree.calls} == {factory.alpha, factory.beta, factory.gamma}
    snap = reader.poll()
    assert snap is not None
    state = snap.source("tickets")
    assert state is not None and state.state == "ok"
    assert by_label(snap)["alpha"].tickets == fv.TicketProgress(1, 1, (("done", 1),), None, NOW)


def test_ticket_fault_keeps_the_last_progress(factory, fake_tree):
    fake_tree.trees[factory.alpha] = [ticket_row("1.1", epic="epic-alpha", status="done")]
    config = fv.FactoryConfig(**{**factory.config.__dict__, "tickets": True})
    reader = fv.FactoryReader(config, clock=lambda: NOW)
    reader.poll()
    reader.poll_slow(force=True)
    fake_tree.fail = tickettree.TicketsTimeout("tickets.py status timed out")
    reader.poll_slow(force=True)
    snap = reader.poll()
    assert snap is not None
    alpha = by_label(snap)["alpha"]
    assert alpha.tickets is not None and (alpha.tickets.done, alpha.tickets.total) == (1, 1)
    assert "timed out" in (alpha.tickets.fault or "")
    assert "tickets: " in alpha.faults[0]
    state = snap.source("tickets")
    assert state is not None and state.state == "fault"


TEAMROUTER = {
    "availability": {
        "claude-opus-5-5": {
            "accounts": [
                {"eligible": True, "blockers": []},
                {
                    "eligible": False,
                    "blockers": [{"code": "quota"}],
                    "estimatedAvailableAt": (NOW + 7200) * 1000,
                },
                {
                    "eligible": False,
                    "blockers": [{"code": "provider"}],
                    "estimatedAvailableAt": None,
                },
            ]
        },
        "gpt-6-astra": {"accounts": [{"eligible": False, "blockers": [{"code": "provider"}]}]},
    },
    "accounts": [
        {
            "name": "a",
            "quota": {"purchasedCredits": {"hasCredits": True, "balance": 1234.5}},
            "onExtraUsage": False,
        },
        {"name": "b", "quota": None},
    ],
}
GH_PRS = [
    {
        "number": 7,
        "state": "MERGED",
        "headRefName": "epic/epic-alpha",
        "createdAt": iso(NOW - 5 * H),
        "mergedAt": iso(NOW - 4 * H),
    },
    {
        "number": 9,
        "state": "OPEN",
        "headRefName": "epic/epic-alpha",
        "createdAt": iso(NOW - 0.2 * H),
        "mergedAt": None,
    },
    {
        "number": 8,
        "state": "MERGED",
        "headRefName": "epic/epic-beta",
        "createdAt": iso(NOW - 3 * H),
        "mergedAt": iso(NOW - 0.5 * H),
    },
]


def test_capacity_and_prs_come_from_their_commands(factory, monkeypatch):
    seen: list[list[str]] = []

    def run_json(argv, timeout):
        seen.append(list(argv))
        return {"teamrouter": TEAMROUTER, "gh": GH_PRS}[argv[0]]

    monkeypatch.setattr(fv, "_run_json", run_json)
    config = fv.FactoryConfig(
        **{
            **factory.config.__dict__,
            "capacity_models": (
                ("Opus", "claude-opus-5-5"),
                ("Codex", "gpt-6-astra"),
                ("Gone", "no-such"),
            ),
            "prs_repo": "owner/repo",
        }
    )
    reader = fv.FactoryReader(config, clock=lambda: NOW)
    reader.poll_slow(force=True)
    snap = reader.poll()
    assert snap is not None
    assert seen[0] == ["teamrouter", "status", "--json"]
    assert seen[1][:5] == ["gh", "pr", "list", "--repo", "owner/repo"]
    assert "head:epic/" in seen[1]
    assert snap.capacity is not None
    opus, codex, gone = snap.capacity.pools
    assert (opus.eligible, opus.accounts, opus.next_at) == (1, 2, NOW + 7200)
    assert (codex.eligible, codex.accounts) == (0, 0)  # only another provider's accounts
    assert gone.missing
    assert (snap.capacity.credits, snap.capacity.credit_accounts) == (1234.5, 1)
    assert by_label(snap)["alpha"].pr == fv.EpicPr(9, "OPEN", NOW - 0.2 * H, None)
    assert by_label(snap)["beta"].pr is not None and by_label(snap)["beta"].pr.number == 8
    assert snap.throughput.merged == (1, 2, 2)


def test_a_missing_command_is_a_named_fault(factory, monkeypatch):
    monkeypatch.setattr(fv.shutil, "which", lambda name: None)
    config = fv.FactoryConfig(
        **{
            **factory.config.__dict__,
            "capacity_models": (("Opus", "claude-opus-5-5"),),
            "prs_repo": "o/r",
        }
    )
    reader = fv.FactoryReader(config, clock=lambda: NOW)
    reader.poll_slow(force=True)
    snap = reader.poll()
    assert snap is not None and snap.capacity is None
    tr, gh = snap.source("TeamRouter"), snap.source("gh")
    assert tr is not None and (tr.state, tr.detail) == ("fault", "teamrouter is not on PATH")
    assert gh is not None and (gh.state, gh.detail) == ("fault", "gh is not on PATH")


def test_parse_capacity_refuses_a_document_without_availability():
    with pytest.raises(ValueError, match="availability"):
        fv.parse_capacity({"accounts": []}, (("Opus", "m"),))


# -------------------------------------------------------------- pure helpers


def test_window_counts_and_hourly_buckets_at_their_edges():
    stamps = [NOW - 3600, NOW - 3599.9, NOW + 5, NOW - 24 * 3600 - 1]
    assert (
        fv.count_since(stamps, NOW, 3600) == 3
    )  # the boundary stamp is in; a future one counts as now
    assert fv.count_since(stamps, NOW, None) == 4
    buckets = fv.hourly_buckets(stamps, NOW)
    assert len(buckets) == 24
    assert buckets[-1] == 2 and buckets[-2] == 1  # exactly one hour old falls in the previous hour
    assert sum(buckets) == 3  # older than a day falls off


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (
            ev(1, "story-done", story_key="1.1", commit="abcdef1234567"),
            ("1.1", "story done abcdef1234", "good"),
        ),
        (
            ev(1, "session-end", task_id="1.1-dev-1", status="stalled", tokens_weighted=1_500_000),
            ("1.1-dev-1", "session stalled · 1.5M w", "warn"),
        ),
        (
            ev(1, "run-paused", stage="story-checkpoint", reason="a\nb"),
            ("", "paused at story-checkpoint: a", "warn"),
        ),
        (
            ev(1, "run-paused", stage="environment", reason="x"),
            ("", "paused at environment: x", "alert"),
        ),
        (
            ev(
                1,
                "verify-command-result",
                story_key="1.1",
                returncode=75,
                command="verify-lock nextest",
            ),
            ("1.1", "verify failed (rc 75): verify-lock nextest", "warn"),
        ),
        (
            ev(1, "session-idle", task_id="t", idle_s=600.4, threshold_s=600),
            ("t", "idle 10m (threshold 10m)", "warn"),
        ),
    ],
)
def test_feed_item_words(entry, expected):
    assert fv.feed_item(entry) == expected


@pytest.mark.parametrize(
    "entry",
    [
        ev(1, "verify-command-result", returncode=0),
        ev(1, "dev-decision", action="proceed"),
        ev(1, "plugin-hook", plugin="x"),
    ],
)
def test_feed_item_leaves_out_routine_events(entry):
    assert fv.feed_item(entry) is None
