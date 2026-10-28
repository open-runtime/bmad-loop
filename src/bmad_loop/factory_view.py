"""Read model of the factory overview (``bmad-loop tui --factory``): every epic
workspace of one factory, read at once.

A factory here is a set of bmad-loop projects, one per epic, each a git worktree
with its own ``.bmad-loop/runs/``, launched side by side by a scheduler. A
project's dashboard sees only its own runs, so this module reads them all and
reduces them to one immutable :class:`FactorySnapshot`: a row per epic workspace,
factory-wide throughput (stories done per hour, cycle time, tokens per hour), the
verify locks, the scheduler's roster, and, when configured, ticket progress, pool
capacity and story PRs, plus a merged feed of recent events.

Read-only by construction. The sources are files the engines and the factory's
tools already write (``state.json``, replaced atomically; ``journal.jsonl``,
append-only; ``tasks/<task>/heartbeat.json``; the scheduler's state file; the
verify locks' ``owner.json`` and queue tickets) and three fixed read-only
commands: ``tickets.py status`` through :class:`~bmad_loop.tickettree.TicketTree`,
``teamrouter status --json`` and ``gh pr list``. Nothing here writes, signals or
kills, and no argument of a command comes from a file a session can edit.

Degrading per source. Each source reports a :class:`SourceState` (``ok``,
``absent``, ``off`` when the config leaves it out, ``pending`` before its first
read, ``fault`` with the reason), and a reader that fails keeps its last good
answer beside the fault. A row that could not be read says so in
``EpicRow.faults``; a fault never reads as an empty, healthy value (AGENTS.md,
"A degrade must be visible").

Cadence. :meth:`FactoryReader.poll` is the fast path: files only, stat-gated,
journals read incrementally, milliseconds when nothing changed.
:meth:`FactoryReader.poll_slow` is the slow one: ticket trees when their files
change, TeamRouter every minute, gh every three. Both are safe to call from
worker threads, and the slow answers reach the next fast snapshot. Textual-free:
the TUI's factory screen owns the timers.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import threading
import time
import tomllib
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

from . import bmadconfig, childrun, runs, tickettree
from .adapters.multiplexer import MultiplexerError
from .documents import run_token_totals
from .journal import JOURNAL_FILE, STATE_FILE, load_state
from .model import (
    PAUSE_EPIC_BOUNDARY,
    PAUSE_PLAN_CHECKPOINT,
    PAUSE_SPEC_APPROVAL,
    PAUSE_STORY_CHECKPOINT,
    PAUSE_STORY_GATE,
    PAUSE_TICKET_HITL,
    PAUSE_TICKET_REFINE,
    TERMINAL_PHASES,
    RunState,
)
from .process_host import ProcessHostError

# ------------------------------------------------------------------ constants

#: Where a project keeps its own factory config; the overview reads it before the
#: built-in default.
FACTORY_CONFIG_REL = Path(".bmad-loop") / "factory.toml"
DEFAULT_CONFIG_SOURCE = "built-in default (bmad_loop/data/factory/default.toml)"

#: The windows rates and counts are taken over, in seconds (``None``: everything
#: the retained runs hold).
WINDOWS: tuple[tuple[str, float | None], ...] = (
    ("1h", 3600.0),
    ("6h", 6 * 3600.0),
    ("24h", 24 * 3600.0),
    ("all", None),
)

#: A heartbeat older than this under an open session means the engine's wait loop
#: is not ticking: four of the generic adapter's 30 s heartbeat intervals.
HEARTBEAT_STALE_S = 120.0
#: Transcript silence is flagged at this fraction of the run's ``dev_stall_grace_s``,
#: before the engine itself calls the session idle at the full grace.
QUIET_WARN_FRACTION = 0.5
DEFAULT_STALL_GRACE_S = 600.0
#: Slow sources: how often each is looked at. Ticket trees are re-read only when
#: their files changed since the last look; a failed read is retried on the next.
TICKETS_EVERY_S = 10.0
TICKETS_RETRY_S = 30.0  # a tree read that failed is tried again after this long
CAPACITY_EVERY_S = 60.0
PRS_EVERY_S = 180.0
CAPACITY_TIMEOUT_S = 20.0
PRS_TIMEOUT_S = 30.0
FEED_MAX = 400

#: Pauses that are designed waits for a person (shown as gates), as opposed to
#: escalations, environment faults and tree faults (shown as parks).
GATE_STAGES = frozenset(
    {
        PAUSE_STORY_CHECKPOINT,
        PAUSE_PLAN_CHECKPOINT,
        PAUSE_TICKET_HITL,
        PAUSE_TICKET_REFINE,
        PAUSE_EPIC_BOUNDARY,
        PAUSE_SPEC_APPROVAL,
        PAUSE_STORY_GATE,
    }
)
STALL_STATUSES = frozenset({"stalled", "timeout"})
#: The journal kinds that move a story from one stage to the next, and the run-level
#: ones that do the same for whatever story is current.
STAGE_EVENTS = frozenset(
    {
        "story-start",
        "session-start",
        "session-end",
        "dev-decision",
        "verify-command-result",
        "review-result",
        "review-skipped",
        "review-not-recommended",
        "review-verify-failed",
        "resume-restart",
        "resume-verify",
        "resume-reverify",
        "reverify-decision",
        "story-reverify-armed",
        "baseline-accepted",
        "checkpoint-pause",
        "story-escalated",
        "story-escalation-resolved",
        "story-deferred",
        "story-done",
        "ticket-mark",
    }
)
RUN_STAGE_EVENTS = frozenset({"run-start", "run-paused", "run-resume"})

# The journal fields the overview keeps per event (everything else, prompts and
# output tails included, is dropped on read), and the cap on a kept string.
_KEEP_FIELDS = (
    "ts",
    "kind",
    "story_key",
    "task_id",
    "role",
    "adapter",
    "model",
    "status",
    "tokens_weighted",
    "stage",
    "reason",
    "action",
    "returncode",
    "command",
    "commit",
    "ref",
    "before",
    "after",
    "cycle",
    "idle_s",
    "threshold_s",
    "checkpoint",
    "weighted",
    "budget",
)
_TEXT_CAP = 240
_SMALL_FILE_CAP = 1 << 20  # owner.json, heartbeat.json, the roster: never near this
_LIVE_STATUSES = (runs.RUNNING, runs.UNKNOWN)

_Sig = tuple[int, int, int]


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _first_line(value: object, cap: int = 120) -> str:
    text = str(value or "").strip().splitlines()
    line = text[0].strip() if text else ""
    return line if len(line) <= cap else line[: cap - 1] + "…"


def _epoch(value: object) -> float | None:
    """An ISO 8601 timestamp (``Z`` or an offset; a naive one is local time) as
    epoch seconds, or None when there is none to read."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _real(path: Path | str) -> str:
    return os.path.realpath(path)


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _sig(path: Path) -> tuple[_Sig | None, str | None]:
    """``(sig, None)`` for a present file, ``(None, None)`` for absence and
    ``(None, why)`` when it could not be stat'd."""
    try:
        st = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None, None
    except OSError as e:
        return None, _describe(e)
    return (st.st_mtime_ns, st.st_size, st.st_ino), None


# --------------------------------------------------------------------- config


class FactoryConfigError(Exception):
    """A factory config that cannot be read, or whose ``[factory]`` table is invalid."""


@dataclass(frozen=True)
class FactoryConfig:
    """What the overview reads. Paths are as written, ``~`` expanded and relative
    ones resolved against the config file's folder. Nothing is checked for
    existence here: a missing source is the reader's to report."""

    name: str
    source: str
    workspaces_root: Path
    scheduler_state: Path | None = None
    scheduler_config: Path | None = None
    locks_dir: Path | None = None
    refresh_s: float = 2.0
    tickets: bool = True
    program_root: Path | None = None
    program_initiatives: tuple[str, ...] = ()
    capacity_models: tuple[tuple[str, str], ...] = ()
    prs_repo: str | None = None
    pr_head_prefix: str = "epic/"


_TOP_KEYS = frozenset(
    {
        "name",
        "workspaces_root",
        "scheduler_state",
        "scheduler_config",
        "locks_dir",
        "refresh_s",
        "tickets",
        "program",
        "capacity",
        "prs",
    }
)
_SUB_KEYS = {
    "program": frozenset({"root", "initiatives"}),
    "capacity": frozenset({"models"}),
    "prs": frozenset({"repo", "head_prefix"}),
}


def _no_unknown(table: Mapping[str, Any], allowed: frozenset[str], where: str, src: str) -> None:
    extra = sorted(set(table) - allowed)
    if extra:
        raise FactoryConfigError(f"{src}: unknown key(s) in [{where}]: {', '.join(extra)}")


def _opt_str(table: Mapping[str, Any], key: str, where: str, src: str) -> str | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise FactoryConfigError(f"{src}: [{where}] {key} must be a non-empty string")
    return value.strip()


def _opt_path(value: str | None, base: Path | None) -> Path | None:
    if value is None:
        return None
    path = Path(os.path.expanduser(value))
    if not path.is_absolute() and base is not None:
        path = base / path
    return path


def _sub_table(table: Mapping[str, Any], key: str, src: str) -> Mapping[str, Any]:
    sub = table.get(key, {})
    if not isinstance(sub, dict):
        raise FactoryConfigError(f"{src}: [factory.{key}] must be a table")
    _no_unknown(sub, _SUB_KEYS[key], f"factory.{key}", src)
    return sub


def parse_config(text: str, *, source: str, base: Path | None = None) -> FactoryConfig:
    """Parse a factory config. ``base`` is the folder relative paths resolve
    against (the config file's own); ``source`` names the config in errors. Every
    key lives under ``[factory]``, and an unknown key is an error, not ignored, so
    a typo cannot quietly switch a source off."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise FactoryConfigError(f"{source}: not valid TOML: {e}") from e
    extra = sorted(set(doc) - {"factory"})
    if extra:
        raise FactoryConfigError(
            f"{source}: unknown top-level key(s) {', '.join(extra)}; keys live under [factory]"
        )
    table = doc.get("factory")
    if not isinstance(table, dict):
        raise FactoryConfigError(f"{source}: no [factory] table")
    _no_unknown(table, _TOP_KEYS, "factory", source)
    root_text = _opt_str(table, "workspaces_root", "factory", source)
    if root_text is None:
        raise FactoryConfigError(f"{source}: [factory] workspaces_root is required")
    root = _opt_path(root_text, base)
    assert root is not None
    name = _opt_str(table, "name", "factory", source) or root.parent.name or "factory"
    refresh = table.get("refresh_s", 2.0)
    if isinstance(refresh, bool) or not isinstance(refresh, (int, float)):
        raise FactoryConfigError(f"{source}: [factory] refresh_s must be a number of seconds")
    if not 0.5 <= float(refresh) <= 60.0:
        raise FactoryConfigError(f"{source}: [factory] refresh_s must be between 0.5 and 60")
    tickets = table.get("tickets", True)
    if not isinstance(tickets, bool):
        raise FactoryConfigError(f"{source}: [factory] tickets must be true or false")

    program = _sub_table(table, "program", source)
    program_root = _opt_path(_opt_str(program, "root", "factory.program", source), base)
    initiatives_raw = program.get("initiatives", [])
    if not isinstance(initiatives_raw, list) or not all(
        isinstance(i, str) and i.strip() for i in initiatives_raw
    ):
        raise FactoryConfigError(
            f"{source}: [factory.program] initiatives must be a list of initiative names"
        )
    initiatives = tuple(str(i).strip() for i in initiatives_raw)
    if initiatives and program_root is None:
        raise FactoryConfigError(f"{source}: [factory.program] initiatives needs a root")

    capacity = _sub_table(table, "capacity", source)
    models_raw = capacity.get("models", {})
    if not isinstance(models_raw, dict) or not all(
        isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()
        for k, v in models_raw.items()
    ):
        raise FactoryConfigError(
            f"{source}: [factory.capacity] models must map a label to a TeamRouter model id"
        )
    models = tuple((str(k).strip(), str(v).strip()) for k, v in models_raw.items())

    prs = _sub_table(table, "prs", source)
    repo = _opt_str(prs, "repo", "factory.prs", source)
    if repo is not None and (repo.count("/") != 1 or repo.startswith("-")):
        raise FactoryConfigError(f"{source}: [factory.prs] repo must be OWNER/NAME")
    prefix = prs.get("head_prefix", "epic/")
    if not isinstance(prefix, str) or prefix.startswith("-"):
        raise FactoryConfigError(f"{source}: [factory.prs] head_prefix must be a branch prefix")

    return FactoryConfig(
        name=name,
        source=source,
        workspaces_root=root,
        scheduler_state=_opt_path(_opt_str(table, "scheduler_state", "factory", source), base),
        scheduler_config=_opt_path(_opt_str(table, "scheduler_config", "factory", source), base),
        locks_dir=_opt_path(_opt_str(table, "locks_dir", "factory", source), base),
        refresh_s=float(refresh),
        tickets=tickets,
        program_root=program_root,
        program_initiatives=initiatives,
        capacity_models=models,
        prs_repo=repo,
        pr_head_prefix=prefix,
    )


def load_config(path: Path) -> FactoryConfig:
    """Read and parse the factory config at ``path``."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise FactoryConfigError(f"cannot read {path}: {_describe(e)}") from e
    return parse_config(text, source=str(path), base=path.parent)


def default_config() -> FactoryConfig:
    """The built-in config: this fork's own factory (``data/factory/default.toml``)."""
    text = (
        resources.files("bmad_loop.data")
        .joinpath("factory")
        .joinpath("default.toml")
        .read_text(encoding="utf-8")
    )
    return parse_config(text, source=DEFAULT_CONFIG_SOURCE)


def resolve_config(project: Path, explicit: Path | None = None) -> FactoryConfig:
    """The config the overview opens with: ``explicit`` when given (it must be
    readable), else ``<project>/.bmad-loop/factory.toml`` when that file exists,
    else the built-in default. A project file that exists but will not parse is an
    error, never silently replaced by the default."""
    if explicit is not None:
        return load_config(explicit)
    local = project / FACTORY_CONFIG_REL
    try:
        present = local.stat() is not None
    except OSError:
        present = False
    if present:
        return load_config(local)
    return default_config()


# ------------------------------------------------------------------ snapshot


@dataclass(frozen=True)
class SourceState:
    """One source's health: ``ok``, ``absent`` (the file or folder is not there),
    ``off`` (the config leaves it out), ``pending`` (not read yet) or ``fault``.
    ``read_at`` is the epoch of its last good read, for the slow sources."""

    name: str
    state: str
    detail: str = ""
    read_at: float | None = None


@dataclass(frozen=True)
class TicketProgress:
    """An epic's tickets in its workspace's tree: done of total (dropped tickets
    left out), the count per state, and the read's fault and time."""

    done: int
    total: int
    by_state: tuple[tuple[str, int], ...] = ()
    fault: str | None = None
    read_at: float | None = None


@dataclass(frozen=True)
class ProgramLine:
    """One initiative of the main checkout's tree: tickets done of total, epics."""

    initiative: str
    done: int
    total: int
    epics: int
    fault: str | None = None


@dataclass(frozen=True)
class RosterEntry:
    """The scheduler's view of one epic (``scheduler-state.json``, schema 2)."""

    key: str
    status: str
    reason: str
    epic: str | None = None
    epic_id: str | None = None
    initiative: str | None = None
    branch: str | None = None
    launches: int | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class LockHolder:
    name: str
    slot: int
    pid: int | None
    cwd: str
    command: str
    since: float | None
    lock_class: str = ""
    workspace: str | None = None  # the epic workspace key whose tree holds cwd


@dataclass(frozen=True)
class LockWaiter:
    name: str
    place: int
    pid: int | None
    cwd: str
    since: float | None
    lock_class: str = ""
    workspace: str | None = None


@dataclass(frozen=True)
class PoolCapacity:
    label: str
    model: str
    eligible: int
    accounts: int
    next_at: float | None = None  # earliest estimated availability of a blocked account
    missing: bool = False  # TeamRouter reported no such model


@dataclass(frozen=True)
class Capacity:
    pools: tuple[PoolCapacity, ...]
    credits: float | None  # summed purchased-credit balance, None when no account holds credits
    credit_accounts: int = 0
    on_extra_usage: bool = False


@dataclass(frozen=True)
class EpicPr:
    number: int
    state: str
    created_at: float | None
    merged_at: float | None


@dataclass(frozen=True)
class FeedEntry:
    ts: float
    epic: str
    kind: str
    subject: str
    text: str
    level: str  # info | good | warn | alert


@dataclass(frozen=True)
class EpicRow:
    """One epic workspace, as the overview shows it."""

    key: str  # the workspace's resolved path: the row key (run ids repeat across workspaces)
    workspace: Path
    label: str
    epic_id: str | None
    run_id: str | None
    status: str  # a runs status, or NO_RUN
    paused_stage: str
    story: str | None
    phase: str | None
    since: float | None
    attempt: int | None
    review_cycle: int | None
    agent: str | None
    quiet_s: float | None
    quiet_warn_s: float
    heartbeat_age_s: float | None
    nudges: int | None
    quota_waiting: bool
    lock: str | None
    done_total: int
    done_window: tuple[int, ...]  # one count per WINDOWS entry
    tickets: TicketProgress | None
    stalls: int
    parks: int
    gates: int
    tokens_weighted: int
    last_event: float | None
    roster: RosterEntry | None
    pr: EpicPr | None
    health: str  # ok | warn | alert | idle
    notes: tuple[str, ...] = ()
    faults: tuple[str, ...] = ()


@dataclass(frozen=True)
class Throughput:
    done: tuple[int, ...]  # stories done per WINDOWS entry
    rates: tuple[float, ...]  # per hour, for the timed windows
    hourly: tuple[int, ...]  # stories done in each of the last 24 hours, oldest first
    cycle_median_s: float | None  # first start to done, over stories done in 24 h
    cycle_count: int
    tokens_6h: int  # weighted tokens of sessions that ended in the last 6 h
    stalls_24h: int
    parks_24h: int
    merged: tuple[int, ...] | None = None  # story PRs merged in 1 h / 6 h / 24 h (gh)


@dataclass(frozen=True)
class FactorySnapshot:
    taken_at: float
    name: str
    config_source: str
    rows: tuple[EpicRow, ...]
    throughput: Throughput
    running: int
    ceiling: int | None
    locks: tuple[LockHolder, ...]
    waiters: tuple[LockWaiter, ...]
    capacity: Capacity | None
    capacity_read_at: float | None
    roster_updated_at: float | None
    roster_counts: tuple[tuple[str, int], ...]
    program: tuple[ProgramLine, ...]
    feed: tuple[FeedEntry, ...]
    sources: tuple[SourceState, ...]

    def source(self, name: str) -> SourceState | None:
        return next((s for s in self.sources if s.name == name), None)


NO_RUN = "no run"


# --------------------------------------------------------------- file readers


class _StatFile:
    """One small file parsed on change, gated on ``(mtime_ns, size, inode)``. The
    last good parse is kept across a failed one, which ``fault`` then names;
    ``present`` tells absence from a fault."""

    def __init__(self, path: Path, parse: Callable[[bytes], Any]):
        self.path = path
        self._parse = parse
        self._sig: _Sig | None = None
        self.value: Any = None
        self.present = False
        self.fault: str | None = None

    def read(self) -> Any:
        sig, stat_fault = _sig(self.path)
        if stat_fault is not None:
            self.fault = f"cannot stat {self.path} ({stat_fault})"
            return self.value
        if sig is None:
            self.present, self.value, self._sig, self.fault = False, None, None, None
            return None
        self.present = True
        if sig == self._sig:
            return self.value
        try:
            with self.path.open("rb") as f:
                raw = f.read(_SMALL_FILE_CAP)
            value = self._parse(raw)
        except (OSError, ValueError) as e:
            self.fault = f"{self.path} unreadable ({_describe(e)})"
            return self.value
        self.value, self._sig, self.fault = value, sig, None
        return value


def _parse_json(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8"))


def _parse_toml(raw: bytes) -> Any:
    return tomllib.loads(raw.decode("utf-8"))


class _Journal:
    """Incremental ``journal.jsonl`` reader: the offset only advances past complete
    lines (an append still being flushed is read on a later look), a shrink or a
    new inode starts over, and a complete line that will not parse is counted in
    ``unreadable`` rather than dropped silently. Kept entries carry only
    ``_KEEP_FIELDS``."""

    def __init__(self, path: Path):
        self.path = path
        self.entries: list[dict[str, Any]] = []
        self.unreadable = 0
        self.fault: str | None = None
        self._offset = 0
        self._ino: int | None = None

    def _reset(self) -> None:
        self.entries, self.unreadable, self._offset = [], 0, 0

    def read_new(self) -> bool:
        """Read what was appended since the last look; True when anything changed."""
        sig, stat_fault = _sig(self.path)
        if stat_fault is not None:
            self.fault = f"journal.jsonl cannot be stat'd ({stat_fault})"
            return False
        self.fault = None
        if sig is None:
            changed = bool(self.entries or self.unreadable)
            self._reset()
            self._ino = None
            return changed
        _mtime, size, ino = sig
        changed = False
        if (self._ino is not None and ino != self._ino) or size < self._offset:
            self._reset()
            changed = True
        self._ino = ino
        if size == self._offset:
            return changed
        try:
            with self.path.open("rb") as f:
                f.seek(self._offset)
                chunk = f.read(size - self._offset)
        except OSError as e:
            self.fault = f"journal.jsonl unreadable ({_describe(e)})"
            return changed
        complete = chunk.rfind(b"\n") + 1
        if complete == 0:
            return changed
        self._offset += complete
        for raw in chunk[:complete].splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                entry = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                self.unreadable += 1
                continue
            if isinstance(entry, dict):
                self.entries.append(_trim(entry))
        return True


def _trim(entry: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _KEEP_FIELDS:
        if key in entry:
            value = entry[key]
            if isinstance(value, str) and len(value) > _TEXT_CAP:
                value = value[: _TEXT_CAP - 1] + "…"
            out[key] = value
    return out


def _ts(entry: Mapping[str, Any]) -> float | None:
    value = entry.get("ts")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class _Run:
    """One run dir: its state (stat-gated, last good kept) and its journal."""

    def __init__(self, run_dir: Path):
        self.dir = run_dir
        self.journal = _Journal(run_dir / JOURNAL_FILE)
        self.state: RunState | None = None
        self.state_fault: str | None = None
        self.tokens_weighted = 0
        self._state_sig: _Sig | None = None
        self._suspect: _Sig | None = None

    def refresh(self) -> bool:
        self._refresh_state()
        return self.journal.read_new()

    def _refresh_state(self) -> None:
        sig, stat_fault = _sig(self.dir / STATE_FILE)
        if stat_fault is not None:
            self.state_fault = f"state.json cannot be stat'd ({stat_fault})"
            return
        if sig is None:
            if self.state is not None:
                self.state_fault = "state.json is gone"
            return
        if sig == self._state_sig:
            self.state_fault = None
            return
        try:
            state = load_state(self.dir)
        except (OSError, ValueError, KeyError, TypeError) as e:
            # A torn read heals on the next write; the same bad file seen twice is not torn.
            if self._suspect == sig:
                self.state_fault = f"state.json unreadable ({_describe(e)})"
            self._suspect = sig
            return
        self.state, self._state_sig, self.state_fault, self._suspect = state, sig, None, None
        try:
            self.tokens_weighted = run_token_totals(state)[1]
        except (AttributeError, TypeError, ValueError):
            self.tokens_weighted = 0


@dataclass(frozen=True)
class _Facts:
    """What an epic's journals (every retained run, oldest first) add up to."""

    done_at: Mapping[str, float] = field(default_factory=dict)
    first_start: Mapping[str, float] = field(default_factory=dict)
    session_ends: tuple[tuple[float, str, int], ...] = ()
    pauses: tuple[tuple[float, str], ...] = ()
    last_ts: float | None = None
    unreadable: int = 0


def _facts(run_list: Iterable[_Run]) -> _Facts:
    done_at: dict[str, float] = {}
    first: dict[str, float] = {}
    ends: list[tuple[float, str, int]] = []
    pauses: list[tuple[float, str]] = []
    last: float | None = None
    unreadable = 0
    for run in run_list:
        unreadable += run.journal.unreadable
        for e in run.journal.entries:
            ts = _ts(e)
            if ts is None:
                continue
            last = ts if last is None else max(last, ts)
            kind = e.get("kind")
            story = e.get("story_key")
            if kind in ("story-start", "session-start") and isinstance(story, str) and story:
                first[story] = min(first.get(story, ts), ts)
            elif kind == "story-done" and isinstance(story, str) and story:
                done_at[story] = max(done_at.get(story, ts), ts)
            elif kind == "session-end":
                weighted = e.get("tokens_weighted")
                ends.append(
                    (ts, str(e.get("status") or ""), weighted if isinstance(weighted, int) else 0)
                )
            elif kind == "run-paused":
                pauses.append((ts, str(e.get("stage") or "")))
    return _Facts(done_at, first, tuple(ends), tuple(pauses), last, unreadable)


class _Workspace:
    def __init__(self, path: Path, key: str):
        self.path = path
        self.key = key
        self.runs: dict[str, _Run] = {}
        self.infos: list[runs.RunInfo] = []
        self.listing_fault: str | None = None
        self.facts = _Facts()
        self.heartbeat: _StatFile | None = None


# ----------------------------------------------------------------- metrics


def count_since(stamps: Iterable[float], now: float, window_s: float | None) -> int:
    """How many of ``stamps`` fall in the last ``window_s`` seconds (all of them
    for ``None``); a stamp in the future (clock skew) counts as now."""
    if window_s is None:
        return sum(1 for _ in stamps)
    start = now - window_s
    return sum(1 for ts in stamps if ts >= start)


def hourly_buckets(stamps: Iterable[float], now: float, hours: int = 24) -> tuple[int, ...]:
    """Counts per hour over the last ``hours`` hours, oldest first; bucket ``i``
    covers ``[now - (hours - i) h, now - (hours - i - 1) h)``, the last one ending now."""
    buckets = [0] * hours
    for ts in stamps:
        age = now - ts
        if age < 0:
            age = 0.0
        index = hours - 1 - int(age // 3600)
        if 0 <= index < hours:
            buckets[index] += 1
    return tuple(buckets)


def _throughput(
    facts: Sequence[_Facts], merged_at: Sequence[float] | None, now: float
) -> Throughput:
    done_ts = [ts for f in facts for ts in f.done_at.values()]
    done = tuple(count_since(done_ts, now, window) for _label, window in WINDOWS)
    rates = tuple(
        done[i] / (window / 3600.0) for i, (_label, window) in enumerate(WINDOWS) if window
    )
    day = now - 24 * 3600.0
    cycles = [
        f.done_at[k] - f.first_start[k]
        for f in facts
        for k in f.done_at
        if k in f.first_start and f.done_at[k] >= day and f.done_at[k] >= f.first_start[k]
    ]
    six = now - 6 * 3600.0
    return Throughput(
        done=done,
        rates=rates,
        hourly=hourly_buckets(done_ts, now),
        cycle_median_s=statistics.median(cycles) if cycles else None,
        cycle_count=len(cycles),
        tokens_6h=sum(w for f in facts for ts, _s, w in f.session_ends if ts >= six),
        stalls_24h=sum(
            1
            for f in facts
            for ts, status, _w in f.session_ends
            if ts >= day and status in STALL_STATUSES
        ),
        parks_24h=sum(
            1 for f in facts for ts, stage in f.pauses if ts >= day and stage not in GATE_STAGES
        ),
        merged=(
            None
            if merged_at is None
            else tuple(count_since(merged_at, now, w) for _l, w in WINDOWS[:3])
        ),
    )


# -------------------------------------------------------------------- feed


def _agent_text(entry: Mapping[str, Any]) -> str:
    adapter = str(entry.get("adapter") or "")
    model = str(entry.get("model") or "")
    return f"{adapter}·{model}" if adapter and model else adapter or model or "?"


def _short_age(seconds: object) -> str:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return "?"
    minutes = max(0, int(seconds // 60))
    return f"{minutes}m" if minutes < 60 else f"{minutes // 60}h{minutes % 60:02d}m"


def tokens_text(value: int | float) -> str:
    """Compact token count: 812, 41.2k, 11.3M."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(int(value))


def feed_item(entry: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """``(subject, text, level)`` for a journal event the feed shows, or None for
    one it leaves out (plugin hooks, verify passes and the engine's bookkeeping)."""
    kind = entry.get("kind")
    story = str(entry.get("story_key") or "")
    task = str(entry.get("task_id") or "")
    if kind == "story-start":
        return story, "story started", "info"
    if kind == "story-done":
        return story, f"story done {str(entry.get('commit') or '')[:10]}".rstrip(), "good"
    if kind == "story-escalated":
        return story, f"escalated: {_first_line(entry.get('reason'))}", "alert"
    if kind == "story-deferred":
        return story, f"deferred: {_first_line(entry.get('reason'))}", "warn"
    if kind == "session-start":
        return task, f"session started ({_agent_text(entry)})", "info"
    if kind == "session-end":
        status = str(entry.get("status") or "?")
        weighted = entry.get("tokens_weighted")
        text = f"session {status}"
        if isinstance(weighted, int) and not isinstance(weighted, bool):
            text += f" · {tokens_text(weighted)} w"
        return task, text, "info" if status == "completed" else "warn"
    if kind == "session-idle":
        return (
            task,
            f"idle {_short_age(entry.get('idle_s'))} (threshold {_short_age(entry.get('threshold_s'))})",
            "warn",
        )
    if kind == "session-active":
        return task, "active again", "info"
    if kind == "run-paused":
        stage = str(entry.get("stage") or "?")
        return (
            "",
            f"paused at {stage}: {_first_line(entry.get('reason'))}",
            "warn" if stage in GATE_STAGES else "alert",
        )
    if kind == "run-resume":
        return "", "run resumed", "info"
    if kind == "run-start":
        return "", "run started", "info"
    if kind == "run-stop":
        return "", "run stopped", "warn"
    if kind == "verify-command-result":
        rc = entry.get("returncode")
        if rc in (0, None):
            return None
        return story, f"verify failed (rc {rc}): {_first_line(entry.get('command'), 80)}", "warn"
    if kind == "review-result":
        return (
            story,
            f"review cycle {entry.get('cycle') or '?'}: {entry.get('status') or '?'}",
            "info",
        )
    if kind == "ticket-mark":
        return (
            str(entry.get("ref") or story),
            f"ticket {entry.get('before') or '?'} → {entry.get('after') or '?'}",
            "info",
        )
    if kind == "dev-decision":
        action = str(entry.get("action") or "")
        if action in ("", "proceed"):
            return None
        return (
            story,
            f"dev decision: {action} {_first_line(entry.get('reason'), 80)}".rstrip(),
            "warn",
        )
    if kind == "token-budget-exceeded":
        weighted, budget = entry.get("weighted"), entry.get("budget")
        if isinstance(weighted, int) and isinstance(budget, int):
            return (
                story,
                f"over token budget ({tokens_text(weighted)}/{tokens_text(budget)} w)",
                "warn",
            )
        return story, "over token budget", "warn"
    if kind == "checkpoint-pause":
        return story, f"{entry.get('checkpoint') or ''} checkpoint".strip(), "warn"
    if kind in ("resume-restart", "resume-verify", "resume-reverify"):
        return story, kind.replace("-", " "), "info"
    return None


# ------------------------------------------------------------------ readers


class _SourceFault(Exception):
    pass


def _run_json(argv: Sequence[str], timeout: float) -> Any:
    """Run a fixed read-only command (resolved on PATH, no shell) and parse its
    stdout as JSON; every failure is a :class:`_SourceFault` naming it."""
    exe = shutil.which(argv[0])
    if exe is None:
        raise _SourceFault(f"{argv[0]} is not on PATH")
    try:
        run = childrun.run_argv([exe, *argv[1:]], cwd=None, timeout=timeout, encoding="utf-8")
    except (OSError, ValueError, subprocess.SubprocessError, ProcessHostError) as e:
        raise _SourceFault(f"cannot run {argv[0]}: {_describe(e)}") from e
    if run.timed_out:
        raise _SourceFault(f"{argv[0]} timed out after {timeout:.0f}s")
    if run.returncode != 0:
        raise _SourceFault(
            f"{argv[0]} exited {run.returncode}: {_first_line(run.stderr) or 'no message'}"
        )
    try:
        return json.loads(run.stdout)
    except ValueError as e:
        raise _SourceFault(f"{argv[0]} printed no JSON ({e})") from e


def parse_capacity(doc: Any, models: Sequence[tuple[str, str]]) -> Capacity:
    """Pool capacity from ``teamrouter status --json``: per configured model, the
    eligible accounts of that model's own provider (an account blocked only
    because it belongs to another provider is not counted), and the summed
    purchased-credit balance."""
    if not isinstance(doc, dict):
        raise ValueError("teamrouter status is not a JSON object")
    availability = doc.get("availability")
    if not isinstance(availability, dict):
        raise ValueError("teamrouter status has no availability table")
    pools: list[PoolCapacity] = []
    for label, model in models:
        entry = availability.get(model)
        if not isinstance(entry, dict):
            pools.append(PoolCapacity(label, model, 0, 0, None, missing=True))
            continue
        own = [
            a
            for a in entry.get("accounts") or []
            if isinstance(a, dict)
            and not any(
                isinstance(b, dict) and b.get("code") == "provider" for b in a.get("blockers") or []
            )
        ]
        waits = [
            float(a["estimatedAvailableAt"]) / 1000.0
            for a in own
            if a.get("eligible") is not True
            and isinstance(a.get("estimatedAvailableAt"), (int, float))
            and not isinstance(a.get("estimatedAvailableAt"), bool)
        ]
        pools.append(
            PoolCapacity(
                label,
                model,
                sum(1 for a in own if a.get("eligible") is True),
                len(own),
                min(waits) if waits else None,
            )
        )
    credits = 0.0
    holders = 0
    on_extra = False
    for account in doc.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        quota = account.get("quota")
        purchased = quota.get("purchasedCredits") if isinstance(quota, dict) else None
        if isinstance(purchased, dict) and purchased.get("hasCredits") is True:
            balance = purchased.get("balance")
            if isinstance(balance, (int, float)) and not isinstance(balance, bool):
                credits += float(balance)
                holders += 1
        if account.get("onExtraUsage") is True:
            on_extra = True
    return Capacity(tuple(pools), credits if holders else None, holders, on_extra)


def parse_prs(rows: Any) -> tuple[dict[str, tuple[EpicPr, ...]], tuple[float, ...]]:
    """``gh pr list --json number,state,headRefName,createdAt,mergedAt`` rows as
    PRs per head branch (newest first) and every merge time."""
    if not isinstance(rows, list):
        raise ValueError("gh pr list did not print a list")
    by_head: dict[str, list[EpicPr]] = {}
    merged: list[float] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("number"), int):
            continue
        pr = EpicPr(
            int(row["number"]),
            str(row.get("state") or "?"),
            _epoch(row.get("createdAt")),
            _epoch(row.get("mergedAt")),
        )
        by_head.setdefault(str(row.get("headRefName") or ""), []).append(pr)
        if pr.merged_at is not None:
            merged.append(pr.merged_at)
    return (
        {
            head: tuple(sorted(prs, key=lambda p: (p.created_at or 0.0, p.number), reverse=True))
            for head, prs in by_head.items()
        },
        tuple(sorted(merged)),
    )


def _lock_record(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as f:
            doc = json.loads(f.read(_SMALL_FILE_CAP).decode("utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def read_locks(
    lock_dir: Path, workspaces: Mapping[str, Path]
) -> tuple[tuple[LockHolder, ...], tuple[LockWaiter, ...], SourceState]:
    """The verify locks' holders (``NAME.lock`` and ``NAME@k.lock`` folders with
    an ``owner.json``) and queues (``NAME.queue/*.json`` tickets, arrival order),
    each matched to the epic workspace whose tree holds its cwd. A lock folder
    caught mid-creation (no ``owner.json`` yet) is skipped; one whose record will
    not parse is named in the source's detail."""
    try:
        names = sorted(os.listdir(lock_dir))
    except FileNotFoundError:
        return (), (), SourceState("locks", "absent", f"{lock_dir} does not exist")
    except OSError as e:
        return (), (), SourceState("locks", "fault", f"cannot list {lock_dir} ({_describe(e)})")
    roots = {key: key for key in workspaces}

    def owner_of(cwd: str) -> str | None:
        if not cwd:
            return None
        real = _real(cwd)
        return next((key for key in roots if _within(real, key)), None)

    holders: list[LockHolder] = []
    waiters: list[LockWaiter] = []
    bad: list[str] = []
    for entry in names:
        path = lock_dir / entry
        if entry.endswith(".lock") and not entry.startswith("."):
            stem = entry[: -len(".lock")]
            name, _, slot_text = stem.partition("@")
            if not (path / "owner.json").exists():
                continue
            record = _lock_record(path / "owner.json")
            if record is None:
                bad.append(entry)
                continue
            cwd = str(record.get("cwd") or "")
            holders.append(
                LockHolder(
                    name=str(record.get("name") or name),
                    slot=int(slot_text) if slot_text.isdigit() else 1,
                    pid=_int_or_none(record.get("pid")),
                    cwd=cwd,
                    command=str(record.get("command") or ""),
                    since=_epoch(record.get("acquired_at")),
                    lock_class=str(record.get("class") or ""),
                    workspace=owner_of(cwd),
                )
            )
        elif entry.endswith(".queue") and not entry.startswith("."):
            name = entry[: -len(".queue")]
            try:
                tickets = sorted(
                    t for t in os.listdir(path) if t.endswith(".json") and not t.startswith(".")
                )
            except OSError:
                bad.append(entry)
                continue
            for place, ticket in enumerate(tickets, start=1):
                record = _lock_record(path / ticket)
                if record is None:
                    continue  # a waiter leaving between the listing and the read
                cwd = str(record.get("cwd") or "")
                waiters.append(
                    LockWaiter(
                        name=name,
                        place=place,
                        pid=_int_or_none(record.get("pid")),
                        cwd=cwd,
                        since=_epoch(record.get("enqueued_at")),
                        lock_class=str(record.get("class") or ""),
                        workspace=owner_of(cwd),
                    )
                )
    state = (
        SourceState("locks", "fault", "unreadable: " + ", ".join(bad))
        if bad
        else SourceState("locks", "ok")
    )
    return tuple(holders), tuple(waiters), state


def roster_entries(doc: Any) -> tuple[dict[str, RosterEntry], float | None, str | None]:
    """The scheduler roster per workspace key (resolved path), its ``updated_at``
    and why it could not be read, from ``scheduler-state.json`` (schema 2: an
    ``epics`` table keyed ``<initiative>/<epic>``)."""
    if doc is None:
        return {}, None, None
    if not isinstance(doc, dict) or not isinstance(doc.get("epics"), dict):
        return {}, None, "no epics table"
    out: dict[str, RosterEntry] = {}
    for key, entry in doc["epics"].items():
        if not isinstance(entry, dict) or not isinstance(entry.get("workspace"), str):
            continue
        raw_last = entry.get("last_status")
        last: Mapping[str, Any] = raw_last if isinstance(raw_last, dict) else {}
        epic_id = entry.get("epic_id")
        out[_real(entry["workspace"])] = RosterEntry(
            key=str(key),
            status=str(entry.get("status") or "?"),
            reason=_first_line(entry.get("reason"), 200),
            epic=str(entry["epic"]) if isinstance(entry.get("epic"), str) else None,
            epic_id=None if epic_id is None or isinstance(epic_id, bool) else str(epic_id),
            initiative=(
                str(entry["initiative"]) if isinstance(entry.get("initiative"), str) else None
            ),
            branch=str(entry["branch"]) if isinstance(entry.get("branch"), str) else None,
            launches=_int_or_none(entry.get("launches")),
            run_id=str(last["run_id"]) if isinstance(last.get("run_id"), str) else None,
        )
    return out, _epoch(doc.get("updated_at")), None


# ----------------------------------------------------------- ticket trees


@dataclass(frozen=True)
class _TicketTarget:
    root: Path  # passed to tickets.py as --project-root
    pinned: str | None  # the run's pinned initiative
    folder: Path | None  # None: the project's active initiative folder
    epic: str


def _tree_sig(root: Path, folder: Path) -> object:
    """What a ``tickets.py status`` answer depends on: every ``tickets.toml`` and
    ``*.md`` under the initiative folder (four levels), the script, and the config
    layers ``active_initiative`` is read from."""
    files: list[tuple[str, object, object]] = []
    pending: list[tuple[Path, int]] = [(folder, 1)]
    while pending:
        here, depth = pending.pop()
        try:
            with os.scandir(here) as it:
                for entry in it:
                    try:
                        if entry.is_dir():
                            if depth < 4:
                                pending.append((Path(entry.path), depth + 1))
                            continue
                        if entry.name != "tickets.toml" and not entry.name.endswith(".md"):
                            continue
                        st = entry.stat()
                    except OSError as e:
                        files.append((entry.path, "error", e.errno))
                        continue
                    files.append((entry.path, st.st_mtime_ns, st.st_size))
        except OSError as e:
            files.append((str(here), "unlistable", e.errno))
    sources = (
        *bmadconfig.V7_CENTRAL_LAYERS_REL,
        bmadconfig.V7_MARKER_REL,
        bmadconfig.V7_CONFIG_UTILS_REL,
    )
    return tuple(sorted(files, key=lambda f: f[0])), tuple(_sig(root / rel) for rel in sources)


def _initiative_folder(root: Path, initiative: str | None) -> tuple[Path, str]:
    """The folder of ``initiative`` (the active one for None) in ``root``'s tree
    and its name: initiatives sit side by side, so another initiative is the
    active folder's sibling."""
    paths = bmadconfig.load_paths(root, want_tickets=True)
    if paths.era != "v7" or paths.initiative_folder is None or not paths.active_initiative:
        raise _SourceFault("not a v7 tickets project ([core] active_initiative)")
    if initiative is None or initiative == paths.active_initiative:
        return paths.initiative_folder, paths.active_initiative
    return paths.initiative_folder.parent / initiative, initiative


# ------------------------------------------------------------------- reader


class FactoryReader:
    """Reads one factory. :meth:`poll` returns a fresh :class:`FactorySnapshot`
    (or None when another poll is still running); :meth:`poll_slow` refreshes the
    ticket trees, capacity and PRs on their own intervals. ``clock`` is the epoch
    clock windows and ages are measured on (tests pin it)."""

    def __init__(self, config: FactoryConfig, *, clock: Callable[[], float] = time.time):
        self.config = config
        self._clock = clock
        self._poll_lock = threading.Lock()
        self._slow_lock = threading.Lock()
        self._shared = threading.Lock()  # slow answers <-> the fast snapshot
        self._workspaces: dict[str, _Workspace] = {}
        self._roster = (
            _StatFile(config.scheduler_state, _parse_json) if config.scheduler_state else None
        )
        self._ceiling = (
            _StatFile(config.scheduler_config, _parse_toml) if config.scheduler_config else None
        )
        self._feed: tuple[FeedEntry, ...] = ()
        # slow side, guarded by _shared
        self._targets: dict[str, _TicketTarget] = {}
        self._tickets: dict[str, TicketProgress] = {}
        self._ticket_sigs: dict[str, object] = {}  # key -> signature of the last good read
        self._ticket_retry: dict[str, float] = {}  # key -> monotonic time a failed read may retry
        self._tickets_state = SourceState(
            "tickets",
            "pending" if config.tickets else "off",
            "" if config.tickets else "tickets = false",
        )
        self._program: tuple[ProgramLine, ...] = ()
        self._program_sigs: dict[str, object] = {}
        self._program_state = SourceState(
            "program",
            "pending" if config.program_initiatives else "off",
            "" if config.program_initiatives else "no [factory.program]",
        )
        self._capacity: Capacity | None = None
        self._capacity_state = SourceState(
            "TeamRouter",
            "pending" if config.capacity_models else "off",
            "" if config.capacity_models else "no [factory.capacity] models",
        )
        self._prs: dict[str, tuple[EpicPr, ...]] | None = None
        self._merged: tuple[float, ...] | None = None
        self._prs_state = SourceState(
            "gh",
            "pending" if config.prs_repo else "off",
            "" if config.prs_repo else "no [factory.prs] repo",
        )
        self._next: dict[str, float] = {}
        self._polled = False  # a fast poll has named the ticket targets

    # ------------------------------------------------------------ fast path

    def poll(self) -> FactorySnapshot | None:
        if not self._poll_lock.acquire(blocking=False):
            return None
        try:
            return self._poll()
        finally:
            self._poll_lock.release()

    def _discover(self, roster: Mapping[str, RosterEntry]) -> tuple[dict[str, Path], SourceState]:
        root = self.config.workspaces_root
        found: dict[str, Path] = {}
        state = SourceState("workspaces", "ok")
        try:
            with os.scandir(root) as it:
                for entry in it:
                    try:
                        if entry.is_dir() and (Path(entry.path) / ".bmad-loop").is_dir():
                            found[_real(entry.path)] = Path(entry.path)
                    except OSError:
                        continue
        except (FileNotFoundError, NotADirectoryError):
            state = SourceState("workspaces", "absent", f"{root} does not exist")
        except OSError as e:
            state = SourceState("workspaces", "fault", f"cannot list {root} ({_describe(e)})")
        for key in roster:
            if key not in found and os.path.isdir(os.path.join(key, ".bmad-loop")):
                found[key] = Path(key)
        return found, state

    def _refresh_workspace(self, ws: _Workspace) -> bool:
        try:
            infos, fault = runs.discover_runs(ws.path)
        except (OSError, ValueError, ProcessHostError, MultiplexerError) as e:
            ws.listing_fault = f"run listing failed ({_describe(e)})"
            infos = ws.infos
        else:
            ws.listing_fault = fault
        ws.infos = infos
        changed = False
        live: set[str] = set()
        for info in infos:
            live.add(info.run_id)
            run = ws.runs.get(info.run_id)
            if run is None:
                run = ws.runs[info.run_id] = _Run(info.run_dir)
                changed = True
            if run.refresh():
                changed = True
        for gone in set(ws.runs) - live:
            del ws.runs[gone]
            changed = True
        if changed:
            ws.facts = _facts(ws.runs[i.run_id] for i in infos if i.run_id in ws.runs)
        return changed

    def _poll(self) -> FactorySnapshot:
        now = self._clock()
        sources: list[SourceState] = []

        roster_doc = self._roster.read() if self._roster is not None else None
        roster, roster_updated, roster_fault = roster_entries(roster_doc)
        if self._roster is None:
            sources.append(SourceState("scheduler", "off", "no scheduler_state"))
        elif self._roster.fault or roster_fault:
            sources.append(
                SourceState("scheduler", "fault", self._roster.fault or roster_fault or "")
            )
        elif not self._roster.present:
            sources.append(
                SourceState("scheduler", "absent", f"{self._roster.path} does not exist")
            )
        else:
            sources.append(SourceState("scheduler", "ok"))

        found, ws_state = self._discover(roster)
        sources.insert(0, ws_state)
        changed = False
        for key, path in found.items():
            ws = self._workspaces.get(key)
            if ws is None:
                ws = self._workspaces[key] = _Workspace(path, key)
                changed = True
            if self._refresh_workspace(ws):
                changed = True
        for gone in set(self._workspaces) - set(found):
            del self._workspaces[gone]
            changed = True
        workspaces = [self._workspaces[k] for k in found]

        if self.config.locks_dir is not None:
            holders, waiters, lock_state = read_locks(self.config.locks_dir, found)
        else:
            holders, waiters, lock_state = (), (), SourceState("locks", "off", "no locks_dir")
        sources.append(lock_state)

        ceiling: int | None = None
        if self._ceiling is not None:
            doc = self._ceiling.read()
            table = doc.get("scheduler") if isinstance(doc, dict) else None
            if isinstance(table, dict):
                ceiling = _int_or_none(table.get("max_parallel_epics"))

        with self._shared:
            tickets = dict(self._tickets)
            capacity, capacity_state = self._capacity, self._capacity_state
            prs, merged, prs_state = self._prs, self._merged, self._prs_state
            program, program_state = self._program, self._program_state
            tickets_state = self._tickets_state
        sources.extend([tickets_state, program_state, capacity_state, prs_state])

        rows = [
            self._row(ws, now, roster.get(ws.key), holders, waiters, tickets.get(ws.key), prs)
            for ws in workspaces
        ]
        rows.sort(key=_row_order)
        with self._shared:
            self._targets = {
                ws.key: target
                for ws in workspaces
                if (target := self._target(ws, roster)) is not None
            }
            self._polled = True
        if changed or not self._feed:
            labels = {row.key: row.label for row in rows}
            self._feed = self._build_feed(workspaces, labels)
        counts = Counter(entry.status for entry in roster.values())
        return FactorySnapshot(
            taken_at=now,
            name=self.config.name,
            config_source=self.config.source,
            rows=tuple(rows),
            throughput=_throughput([ws.facts for ws in workspaces], merged, now),
            running=sum(1 for row in rows if row.status == runs.RUNNING),
            ceiling=ceiling,
            locks=holders,
            waiters=waiters,
            capacity=capacity,
            capacity_read_at=capacity_state.read_at,
            roster_updated_at=roster_updated,
            roster_counts=tuple(sorted(counts.items())),
            program=program,
            feed=self._feed,
            sources=tuple(sources),
        )

    def _target(self, ws: _Workspace, roster: Mapping[str, RosterEntry]) -> _TicketTarget | None:
        """Where the epic's tickets are read: the newest run's pinned initiative and
        folder for a tickets run, else the workspace's active initiative."""
        if not self.config.tickets:
            return None
        newest = ws.runs.get(ws.infos[-1].run_id) if ws.infos else None
        state = newest.state if newest is not None else None
        entry = roster.get(ws.key)
        if state is not None and state.source == "tickets" and state.initiative_folder:
            folder = Path(state.initiative_folder)
            if not folder.is_absolute():
                folder = ws.path / folder
            folder = runs.rebase_recorded_project_path(folder, state, ws.path)
            epic = state.ticket_epic_filter or (entry.epic if entry else None) or ws.path.name
            return _TicketTarget(ws.path, state.initiative or None, folder, epic)
        if state is not None and state.source != "tickets":
            return None
        return _TicketTarget(ws.path, None, None, (entry.epic if entry else None) or ws.path.name)

    def _row(
        self,
        ws: _Workspace,
        now: float,
        roster: RosterEntry | None,
        holders: Sequence[LockHolder],
        waiters: Sequence[LockWaiter],
        tickets: TicketProgress | None,
        prs: Mapping[str, tuple[EpicPr, ...]] | None,
    ) -> EpicRow:
        info = ws.infos[-1] if ws.infos else None
        newest = ws.runs.get(info.run_id) if info is not None else None
        state = newest.state if newest is not None else None
        status = info.status if info is not None else NO_RUN
        paused_stage = info.paused_stage if info is not None else ""
        faults: list[str] = []
        if ws.listing_fault:
            faults.append(ws.listing_fault)
        if newest is not None:
            if newest.state_fault:
                faults.append(newest.state_fault)
            if newest.journal.fault:
                faults.append(newest.journal.fault)
        if ws.facts.unreadable:
            faults.append(f"{ws.facts.unreadable} unreadable journal line(s)")
        if tickets is not None and tickets.fault:
            faults.append(f"tickets: {tickets.fault}")

        story = phase = None
        attempt = review_cycle = None
        epic_id = roster.epic_id if roster is not None else None
        if state is not None:
            key = state.paused_story_key if state.paused_story_key in state.tasks else None
            if key is None:
                for k, task in state.tasks.items():
                    if task.phase not in TERMINAL_PHASES:
                        key = k
            if key is not None:
                task = state.tasks[key]
                story, phase = key, str(task.phase)
                attempt, review_cycle = task.attempt, task.review_cycle
            if epic_id is None:
                epic_id = next(
                    (
                        t.ticket.epic_id
                        for t in state.tasks.values()
                        if t.ticket and t.ticket.epic_id
                    ),
                    None,
                )

        entries = newest.journal.entries if newest is not None else []
        since = _stage_since(entries, story)
        open_session = _open_session(entries) if status in _LIVE_STATUSES else None
        agent = _agent_text(open_session) if open_session is not None else None

        quiet = hb_age = None
        nudges = None
        quota_waiting = False
        if open_session is not None and newest is not None:
            beat = self._heartbeat(ws, newest.dir, str(open_session.get("task_id") or ""))
            started = _ts(open_session) or 0.0
            hb_ts = beat.get("ts") if isinstance(beat, dict) else None
            if isinstance(hb_ts, (int, float)) and not isinstance(hb_ts, bool) and hb_ts >= started:
                hb_age = max(0.0, now - float(hb_ts))
                idle = beat.get("transcript_idle_s")
                if isinstance(idle, (int, float)) and not isinstance(idle, bool):
                    quiet = float(idle) + hb_age
                nudges = _int_or_none(beat.get("stall_nudges_sent"))
                quota_waiting = beat.get("quota_waiting") is True
        grace = _stall_grace(state)
        quiet_warn = QUIET_WARN_FRACTION * grace

        mine = [h for h in holders if h.workspace == ws.key]
        queued = [w for w in waiters if w.workspace == ws.key]
        lock = None
        if mine:
            lock = "holds " + ", ".join(
                f"{h.name}" + (f" {_short_age(now - h.since)}" if h.since else "") for h in mine
            )
        if queued:
            text = ", ".join(
                f"{w.name} #{w.place}" + (f" {_short_age(now - w.since)}" if w.since else "")
                for w in queued
            )
            lock = f"{lock} · waits {text}" if lock else f"waits {text}"
        if (
            lock is None
            and phase is not None
            and phase.endswith("-verify")
            and status == runs.RUNNING
        ):
            lock = "verifying"

        facts = ws.facts
        done_window = tuple(count_since(facts.done_at.values(), now, w) for _l, w in WINDOWS)
        stalls = sum(1 for _ts_, status_, _w in facts.session_ends if status_ in STALL_STATUSES)
        gates = sum(1 for _ts_, stage in facts.pauses if stage in GATE_STAGES)
        parks = len(facts.pauses) - gates
        branch = (roster.branch if roster is not None else None) or (
            f"{self.config.pr_head_prefix}{ws.path.name}"
        )
        pr_list = prs.get(branch) if prs is not None else None
        name = ws.path.name
        label = name[len("epic-") :] if name.startswith("epic-") and len(name) > 5 else name

        health, notes = _health(
            status,
            paused_stage,
            open_session is not None,
            hb_age,
            quiet,
            quiet_warn,
            attempt,
            quota_waiting,
            roster,
        )
        if faults and health in ("ok", "idle"):
            health = "warn"
        return EpicRow(
            key=ws.key,
            workspace=ws.path,
            label=label,
            epic_id=epic_id,
            run_id=info.run_id if info is not None else None,
            status=status,
            paused_stage=paused_stage,
            story=story,
            phase=phase,
            since=since,
            attempt=attempt,
            review_cycle=review_cycle,
            agent=agent,
            quiet_s=quiet,
            quiet_warn_s=quiet_warn,
            heartbeat_age_s=hb_age,
            nudges=nudges,
            quota_waiting=quota_waiting,
            lock=lock,
            done_total=len(facts.done_at),
            done_window=done_window,
            tickets=tickets,
            stalls=stalls,
            parks=parks,
            gates=gates,
            tokens_weighted=sum(r.tokens_weighted for r in ws.runs.values()),
            last_event=facts.last_ts,
            roster=roster,
            pr=pr_list[0] if pr_list else None,
            health=health,
            notes=tuple(notes),
            faults=tuple(faults),
        )

    def _heartbeat(self, ws: _Workspace, run_dir: Path, task_id: str) -> Any:
        if not task_id:
            return None
        # The generic adapter's wait loop rewrites tasks/<task>/heartbeat.json about
        # every 30 s (adapters/generic.py, _write_heartbeat).
        path = run_dir / "tasks" / task_id / "heartbeat.json"
        if ws.heartbeat is None or ws.heartbeat.path != path:
            ws.heartbeat = _StatFile(path, _parse_json)
        return ws.heartbeat.read()

    def _build_feed(
        self, workspaces: Sequence[_Workspace], labels: Mapping[str, str]
    ) -> tuple[FeedEntry, ...]:
        items: list[FeedEntry] = []
        for ws in workspaces:
            label = labels.get(ws.key, ws.path.name)
            for run in ws.runs.values():
                for e in run.journal.entries:
                    ts = _ts(e)
                    shown = feed_item(e) if ts is not None else None
                    if shown is None or ts is None:
                        continue
                    subject, text, level = shown
                    items.append(FeedEntry(ts, label, str(e.get("kind")), subject, text, level))
        items.sort(key=lambda f: f.ts, reverse=True)
        return tuple(items[:FEED_MAX])

    # ------------------------------------------------------------ slow path

    def _due(self, name: str, every: float, force: bool) -> bool:
        now = time.monotonic()
        if not force and now < self._next.get(name, 0.0):
            return False
        self._next[name] = now + every
        return True

    def poll_slow(self, *, force: bool = False) -> None:
        """Refresh the slow sources that are due (all of them with ``force``).
        Returns at once when another slow poll is running."""
        if not self._slow_lock.acquire(blocking=False):
            return
        try:
            if self.config.tickets and self._due("tickets", TICKETS_EVERY_S, force):
                self._read_tickets(force)
            if self.config.program_initiatives and self._due("program", TICKETS_EVERY_S, force):
                self._read_program(force)
            if self.config.capacity_models and self._due("capacity", CAPACITY_EVERY_S, force):
                self._read_capacity()
            if self.config.prs_repo and self._due("prs", PRS_EVERY_S, force):
                self._read_prs()
        finally:
            self._slow_lock.release()

    def _read_tickets(self, force: bool) -> None:
        with self._shared:
            targets = dict(self._targets)
            polled = self._polled
        if not polled:
            self._next.pop(
                "tickets", None
            )  # no targets before the first fast poll: retry next time
            return
        results: dict[str, TicketProgress] = {}
        faults = 0
        for key, target in targets.items():
            with self._shared:
                previous = self._tickets.get(key)
                previous_sig = self._ticket_sigs.get(key)
            if (
                not force
                and previous is not None
                and time.monotonic() < self._ticket_retry.get(key, 0.0)
            ):
                results[key] = previous  # a failed read waits out its retry spacing
                faults += 1
                continue
            try:
                folder, initiative = (
                    (target.folder, target.pinned or "")
                    if target.folder is not None
                    else _initiative_folder(target.root, None)
                )
                sig = (target, _tree_sig(target.root, folder))
                if not force and previous is not None and sig == previous_sig:
                    results[key] = previous
                    continue
                view = tickettree.TicketTree(target.root, pinned_initiative=target.pinned).status(
                    folder
                )
            except (
                tickettree.TicketsError,
                _SourceFault,
                bmadconfig.BmadConfigError,
                OSError,
            ) as e:
                faults += 1
                why = f"{getattr(e, 'kind', type(e).__name__)}: {e}"
                results[key] = (
                    TicketProgress(
                        previous.done, previous.total, previous.by_state, why, previous.read_at
                    )
                    if previous is not None
                    else TicketProgress(0, 0, (), why, None)
                )
                with self._shared:
                    self._ticket_sigs.pop(key, None)
                self._ticket_retry[key] = time.monotonic() + TICKETS_RETRY_S
                continue
            self._ticket_retry.pop(key, None)
            rows = [r for r in view.tickets if r.epic == target.epic]
            counts = Counter(r.state for r in rows)
            progress = TicketProgress(
                done=counts.get("done", 0),
                total=len(rows) - counts.get("dropped", 0),
                by_state=tuple(sorted(counts.items())),
                fault=(
                    None if rows else f"no tickets of {target.epic} in {initiative or 'the tree'}"
                ),
                read_at=self._clock(),
            )
            results[key] = progress
            with self._shared:
                self._ticket_sigs[key] = sig
        with self._shared:
            self._tickets = results
            self._tickets_state = (
                SourceState(
                    "tickets",
                    "fault",
                    f"{faults} of {len(targets)} trees unreadable",
                    self._clock(),
                )
                if faults
                else SourceState("tickets", "ok", "", self._clock())
            )

    def _read_program(self, force: bool) -> None:
        root = self.config.program_root
        assert root is not None
        lines: list[ProgramLine] = []
        faults = 0
        for initiative in self.config.program_initiatives:
            previous = next((p for p in self._program if p.initiative == initiative), None)
            try:
                folder, _name = _initiative_folder(root, initiative)
                sig = _tree_sig(root, folder)
                if (
                    not force
                    and previous is not None
                    and previous.fault is None
                    and (self._program_sigs.get(initiative) == sig)
                ):
                    lines.append(previous)
                    continue
                view = tickettree.TicketTree(root, pinned_initiative=None).status(folder)
            except (
                tickettree.TicketsError,
                _SourceFault,
                bmadconfig.BmadConfigError,
                OSError,
            ) as e:
                faults += 1
                why = f"{getattr(e, 'kind', type(e).__name__)}: {e}"
                lines.append(
                    ProgramLine(initiative, previous.done, previous.total, previous.epics, why)
                    if previous is not None
                    else ProgramLine(initiative, 0, 0, 0, why)
                )
                continue
            counts = Counter(r.state for r in view.tickets)
            lines.append(
                ProgramLine(
                    initiative,
                    counts.get("done", 0),
                    len(view.tickets) - counts.get("dropped", 0),
                    len(view.epics),
                )
            )
            self._program_sigs[initiative] = sig
        with self._shared:
            self._program = tuple(lines)
            self._program_state = (
                SourceState("program", "fault", f"{faults} initiative(s) unreadable", self._clock())
                if faults
                else SourceState("program", "ok", "", self._clock())
            )

    def _read_capacity(self) -> None:
        try:
            capacity = parse_capacity(
                _run_json(["teamrouter", "status", "--json"], CAPACITY_TIMEOUT_S),
                self.config.capacity_models,
            )
        except (_SourceFault, ValueError) as e:
            with self._shared:
                read_at = self._capacity_state.read_at
                self._capacity_state = SourceState("TeamRouter", "fault", str(e), read_at)
            return
        with self._shared:
            self._capacity = capacity
            self._capacity_state = SourceState("TeamRouter", "ok", "", self._clock())

    def _read_prs(self) -> None:
        repo = self.config.prs_repo
        assert repo is not None
        argv = [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--search",
            f"head:{self.config.pr_head_prefix}",
            "--json",
            "number,state,headRefName,createdAt,mergedAt",
            "--limit",
            "300",
        ]
        try:
            prs, merged = parse_prs(_run_json(argv, PRS_TIMEOUT_S))
        except (_SourceFault, ValueError) as e:
            with self._shared:
                read_at = self._prs_state.read_at
                self._prs_state = SourceState("gh", "fault", str(e), read_at)
            return
        with self._shared:
            self._prs, self._merged = prs, merged
            self._prs_state = SourceState("gh", "ok", "", self._clock())


# --------------------------------------------------------------- row helpers


def _row_order(row: EpicRow) -> tuple[float, str]:
    try:
        return float(row.epic_id) if row.epic_id is not None else float("inf"), row.label
    except ValueError:
        return float("inf"), row.label


def _stall_grace(state: RunState | None) -> float:
    snapshot = state.policy_snapshot if state is not None else None
    limits = snapshot.get("limits") if isinstance(snapshot, dict) else None
    grace = limits.get("dev_stall_grace_s") if isinstance(limits, dict) else None
    if isinstance(grace, (int, float)) and not isinstance(grace, bool) and grace > 0:
        return float(grace)
    return DEFAULT_STALL_GRACE_S


def _open_session(entries: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The newest ``session-start`` no ``session-end`` has closed."""
    open_by_task: dict[str, Mapping[str, Any]] = {}
    for e in entries:
        kind = e.get("kind")
        task = e.get("task_id")
        if not isinstance(task, str):
            continue
        if kind == "session-start":
            open_by_task.pop(task, None)
            open_by_task[task] = e
        elif kind == "session-end":
            open_by_task.pop(task, None)
    return next(reversed(open_by_task.values()), None) if open_by_task else None


def _stage_since(entries: Sequence[Mapping[str, Any]], story: str | None) -> float | None:
    """When the current stage began: the latest stage-changing event of the story
    (:data:`STAGE_EVENTS`, matched by key or by the task ids its sessions ran
    under) or of the run itself (:data:`RUN_STAGE_EVENTS`). A session going idle
    or active again, a plugin hook or a budget sample changes no stage."""
    tasks: set[str] = set()
    latest: float | None = None
    for e in entries:
        ts = _ts(e)
        if ts is None:
            continue
        kind = e.get("kind")
        task = e.get("task_id")
        if kind == "session-start" and story is not None and e.get("story_key") == story:
            if isinstance(task, str):
                tasks.add(task)
        mine = (
            story is not None
            and kind in STAGE_EVENTS
            and (e.get("story_key") == story or (isinstance(task, str) and task in tasks))
        )
        if mine or kind in RUN_STAGE_EVENTS:
            latest = ts if latest is None else max(latest, ts)
    return latest


def _health(
    status: str,
    paused_stage: str,
    session_open: bool,
    hb_age: float | None,
    quiet: float | None,
    quiet_warn: float,
    attempt: int | None,
    quota_waiting: bool,
    roster: RosterEntry | None,
) -> tuple[str, list[str]]:
    """``(level, notes)``: alert for a stopped-by-fault engine, an escalation or
    environment park, or a wait loop that stopped ticking; warn for an owner gate,
    long transcript silence, a retry, a quota wait or a scheduler that disagrees;
    idle for a concluded or never-run epic; else ok."""
    order = {"idle": 0, "ok": 1, "warn": 2, "alert": 3}
    level = "ok"
    notes: list[str] = []

    def bump(to: str, note: str) -> None:
        nonlocal level
        if order[to] > order[level]:
            level = to
        notes.append(note)

    if status in (runs.FINISHED, runs.STOPPED):
        level = "idle"
    elif status == NO_RUN:
        level = "idle"
        if roster is not None and roster.status == "launch-failed":
            bump("alert", f"launch failed: {roster.reason}")
    elif status in (runs.CRASHED, runs.INTERRUPTED):
        bump("alert", f"engine {status}")
    elif status == runs.PAUSED:
        if paused_stage in GATE_STAGES:
            bump("warn", f"waiting at {paused_stage}")
        else:
            bump("alert", f"paused at {paused_stage or 'an unknown stage'}")
    elif status == runs.UNKNOWN:
        bump("warn", "engine liveness unknown")
    if status == runs.RUNNING and session_open:
        if hb_age is not None and hb_age > HEARTBEAT_STALE_S:
            bump(
                "alert",
                f"heartbeat {_short_age(hb_age)} old: the engine's wait loop is not ticking",
            )
        if quiet is not None and quiet >= quiet_warn:
            bump("warn", f"transcript quiet {_short_age(quiet)}")
        if quota_waiting:
            bump("warn", "waiting on a usage quota")
    if status == runs.RUNNING and attempt is not None and attempt >= 2:
        bump("warn", f"attempt {attempt}")
    if (
        roster is not None
        and roster.status in ("blocked", "launch-failed")
        and status == runs.RUNNING
    ):
        bump("warn", f"scheduler: {roster.status} ({roster.reason})")
    return level, notes
