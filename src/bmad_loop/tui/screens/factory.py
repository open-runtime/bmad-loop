"""Factory overview: every epic workspace of one factory on one screen.

Read-only, and a modal screen on purpose: while it is open over the dashboard
the app's run-control keys (r, s, e, x, S, D, …) cannot fire, so nothing acts
on the dashboard's own project while the operator looks at other epics. It opens
over the dashboard with `F`, or alone as `bmad-loop tui --factory`. Enter (or
`o`) opens the highlighted epic's own dashboard as a child `bmad-loop tui
--project <workspace>`, with this TUI suspended until it quits, so every
run-control key there acts on that epic's project. The child is the
`bmad-loop` on PATH when there is one: the overview may run from another build
(``uvx --from``), and run control belongs to the installed tool.

Polling: a fast thread worker reads the files every ``refresh_s``
(:meth:`FactoryReader.poll`) and a slow one refreshes the ticket trees, TeamRouter
and gh on their own intervals (:meth:`FactoryReader.poll_slow`). Each snapshot is
applied on the UI thread, and dropped once the screen is gone. A poll that raises
is shown on the sources line instead of taking the TUI down.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from ... import factory_view as fv
from ... import runs
from ..widgets import STATUS_GLYPHS, _format_age

_SPARK = "▁▂▃▄▅▆▇█"
_HEALTH_STYLES = {"ok": "green", "warn": "yellow", "alert": "bold red", "idle": "dim"}
_LEVEL_STYLES = {"info": "", "good": "green", "warn": "yellow", "alert": "bold red"}
_SOURCE_STYLES = {
    "ok": "green",
    "off": "dim",
    "pending": "dim",
    "absent": "yellow",
    "fault": "bold red",
}
_SLOW_TICK_S = 2.0
_FEED_LINES = 200

# (key, label, width); None takes the rest of the row
_COLUMNS: tuple[tuple[str, str, int | None], ...] = (
    ("st", "st", 2),
    ("id", "id", 4),
    ("epic", "epic", 30),
    ("progress", "progress", 14),
    ("done", "done", 5),
    ("now", "now", 8),
    ("stage", "stage", 15),
    ("for", "for", 6),
    ("att", "att", 3),
    ("quiet", "quiet", 7),
    ("lock", "lock", 18),
    ("stl", "stl/pk", 6),
    ("tok", "tok(w)", 7),
    ("pr", "PR", 11),
    ("note", "note", None),
)


def age(seconds: float | None) -> str:
    """``12m``, ``1h05m``; ``—`` when there is no time to show."""
    return "—" if seconds is None else _format_age(seconds)


def sparkline(counts: Sequence[int]) -> str:
    """One block per bucket, scaled to the largest; an empty bucket is the lowest block."""
    top = max(counts, default=0)
    if top <= 0:
        return _SPARK[0] * len(counts)
    return "".join(_SPARK[min(len(_SPARK) - 1, round(c / top * (len(_SPARK) - 1)))] for c in counts)


def progress_cell(progress: fv.TicketProgress | None, done_stories: int, width: int = 7) -> Text:
    """``███▒░░░ 3/8`` from the epic's tickets; without a tree read, the stories its
    runs finished (``4 done``); a ticket read that failed keeps its last answer
    marked with ``?``."""
    if progress is None or (progress.total == 0 and progress.fault):
        return Text(f"{done_stories} done", style="dim")
    if progress.total <= 0:
        return Text("—", style="dim")
    filled = round(width * progress.done / progress.total)
    in_flight = sum(n for state, n in progress.by_state if state in ("in-progress", "review"))
    partial = min(width - filled, 1 if in_flight else 0)
    bar = "█" * filled + "▒" * partial + "░" * (width - filled - partial)
    text = Text(bar, style="green" if progress.done == progress.total else "cyan")
    text.append(f" {progress.done}/{progress.total}")
    if progress.fault:
        text.append("?", style="yellow")
    return text


def _status_cell(row: fv.EpicRow) -> Text:
    style = _HEALTH_STYLES.get(row.health, "")
    if row.status == fv.NO_RUN:
        return Text("·", style="dim")
    glyph = "●" if row.status == runs.RUNNING else STATUS_GLYPHS.get(row.status, "?")
    return Text(glyph, style=style)


def _pr_cell(pr: fv.EpicPr | None) -> Text:
    if pr is None:
        return Text("—", style="dim")
    state = pr.state.upper()
    if state == "MERGED":
        return Text(f"#{pr.number} ✓", style="green")
    if state == "OPEN":
        return Text(f"#{pr.number} open", style="cyan")
    return Text(f"#{pr.number} {state.lower()}", style="dim")


def _note(row: fv.EpicRow) -> Text:
    if row.faults:
        return Text(row.faults[0], style="bold red")
    if row.notes:
        return Text(row.notes[0], style=_HEALTH_STYLES.get(row.health, ""))
    if row.roster is not None and row.status == fv.NO_RUN:
        return Text(f"{row.roster.status}: {row.roster.reason}", style="dim")
    return Text("")


def row_cells(row: fv.EpicRow, now: float, window: int) -> tuple[Text | str, ...]:
    """The table cells of one epic, in ``_COLUMNS`` order; ``window`` indexes
    ``factory_view.WINDOWS`` for the done column."""
    stage = row.paused_stage if row.status == runs.PAUSED and row.paused_stage else row.phase
    quiet = Text(age(row.quiet_s))
    if row.quiet_s is not None and row.quiet_s >= row.quiet_warn_s:
        quiet = Text(f"{age(row.quiet_s)} ▲", style="yellow")
    if row.heartbeat_age_s is not None and row.heartbeat_age_s > fv.HEARTBEAT_STALE_S:
        quiet = Text(f"hb {age(row.heartbeat_age_s)}", style="bold red")
    attempt = f"×{row.attempt}" if row.attempt else "—"
    return (
        _status_cell(row),
        row.epic_id or "—",
        row.label,
        progress_cell(row.tickets, row.done_total),
        str(row.done_window[window]),
        row.story or "—",
        Text(
            stage or ("—" if row.status != fv.NO_RUN else "no run"),
            style=_HEALTH_STYLES.get(row.health, ""),
        ),
        age(None if row.since is None else now - row.since),
        Text(attempt, style="yellow" if (row.attempt or 0) >= 2 else ""),
        quiet,
        row.lock or "—",
        f"{row.stalls}/{row.parks}",
        fv.tokens_text(row.tokens_weighted) if row.tokens_weighted else "—",
        _pr_cell(row.pr),
        _note(row),
    )


def throughput_text(snap: fv.FactorySnapshot) -> Text:
    t = snap.throughput
    labels = [label for label, _w in fv.WINDOWS]
    out = Text()
    out.append("stories done  ", style="bold")
    parts = [f"{labels[i]} {t.done[i]} ({t.rates[i]:.1f}/h)" for i in range(len(t.rates))]
    out.append(" · ".join(parts) + f" · all {t.done[-1]}\n")
    out.append("last 24h      ", style="bold")
    out.append(sparkline(t.hourly), style="cyan")
    out.append("  now\n", style="dim")
    out.append("cycle median  ", style="bold")
    if t.cycle_median_s is None:
        out.append("— (no story done in 24h)\n", style="dim")
    else:
        out.append(
            f"{age(t.cycle_median_s)} over {t.cycle_count} stories (24h, first start to done)\n"
        )
    out.append("tokens        ", style="bold")
    out.append(f"{fv.tokens_text(t.tokens_6h)} w in 6h ({fv.tokens_text(t.tokens_6h / 6)}/h)")
    out.append(f" · stalls {t.stalls_24h} · parks {t.parks_24h} (24h)\n")
    out.append("PRs merged    ", style="bold")
    gh = snap.source("gh")
    if t.merged is None:
        out.append("— " + _source_note(gh), style="dim")
    else:
        out.append(" · ".join(f"{labels[i]} {t.merged[i]}" for i in range(3)))
        out.append(f"   gh {age(_since(snap, gh))}", style="dim")
    return out


def _since(snap: fv.FactorySnapshot, source: fv.SourceState | None) -> float | None:
    if source is None or source.read_at is None:
        return None
    return max(0.0, snap.taken_at - source.read_at)


def _source_note(source: fv.SourceState | None) -> str:
    if source is None:
        return ""
    if source.state == "pending":
        return "(reading…)"
    return f"({source.detail or source.state})"


def _epic_of(snap: fv.FactorySnapshot, key: str | None, cwd: str) -> str:
    row = next((r for r in snap.rows if r.key == key), None)
    if row is not None:
        return row.label
    return Path(cwd).name or cwd or "?"


def capacity_text(snap: fv.FactorySnapshot) -> Text:
    out = Text()
    out.append("engines       ", style="bold")
    ceiling = f" of {snap.ceiling}" if snap.ceiling is not None else ""
    out.append(f"{snap.running} running{ceiling}\n")
    tr = snap.source("TeamRouter")
    if snap.capacity is None:
        out.append("capacity      ", style="bold")
        out.append("— " + _source_note(tr) + "\n", style="dim")
    else:
        for pool in snap.capacity.pools:
            out.append(f"{pool.label[:13]:<14}", style="bold")
            if pool.missing:
                out.append(f"{pool.model} not in TeamRouter\n", style="yellow")
                continue
            style = "green" if pool.eligible else "bold red"
            out.append(f"{pool.eligible}/{pool.accounts} eligible", style=style)
            if pool.next_at is not None and pool.eligible < pool.accounts:
                out.append(
                    f" · next {time.strftime('%a %H:%M', time.localtime(pool.next_at))}",
                    style="dim",
                )
            out.append("\n")
        out.append("credits       ", style="bold")
        cap = snap.capacity
        if cap.credits is None:
            out.append("none\n", style="dim")
        else:
            out.append(f"{cap.credits:,.0f} on {cap.credit_accounts} account(s)")
            out.append(" · in use" if cap.on_extra_usage else " · not in use", style="dim")
            out.append(f"   TeamRouter {age(_since(snap, tr))}", style="dim")
            if tr is not None and tr.state == "fault":
                out.append(" (stale)", style="yellow")
            out.append("\n")
    locks = snap.source("locks")
    if not snap.locks and not snap.waiters:
        out.append("locks         ", style="bold")
        out.append(
            "free" if locks is not None and locks.state == "ok" else _source_note(locks),
            style="dim",
        )
        return out
    for holder in snap.locks:
        name = holder.name + (f"@{holder.slot}" if holder.slot > 1 else "")
        out.append(f"{name[:13]:<14}", style="bold")
        held = age(None if holder.since is None else snap.taken_at - holder.since)
        out.append(f"{_epic_of(snap, holder.workspace, holder.cwd)} {held}")
        command = (
            holder.command.rsplit("/", 1)[-1] if holder.command.startswith("/") else holder.command
        )
        out.append(f" · {command[:40]}\n", style="dim")
    if snap.waiters:
        out.append("waiting       ", style="bold")
        out.append(
            ", ".join(
                f"{w.name} #{w.place} {_epic_of(snap, w.workspace, w.cwd)}" for w in snap.waiters
            )
        )
    return out


def strip_text(snap: fv.FactorySnapshot, poll_fault: str | None) -> Text:
    out = Text()
    out.append("Scheduler  ", style="bold")
    sched = snap.source("scheduler")
    if sched is not None and sched.state != "ok":
        out.append(f"{sched.state}: {sched.detail}", style=_SOURCE_STYLES.get(sched.state, ""))
    else:
        updated = None if snap.roster_updated_at is None else snap.taken_at - snap.roster_updated_at
        out.append(f"roster {age(updated)} old · ")
        out.append(" · ".join(f"{status} {n}" for status, n in snap.roster_counts) or "no epics")
    if snap.program:
        out.append("    Program  ", style="bold")
        out.append(
            " · ".join(
                f"{line.initiative.removeprefix('initiative-')} {line.done}/{line.total}"
                + (f" ({line.epics} epics)" if line.epics else "")
                + (" ?" if line.fault else "")
                for line in snap.program
            )
        )
    out.append("\nSources    ", style="bold")
    for i, source in enumerate(snap.sources):
        if i:
            out.append(" · ")
        out.append(source.name + " ")
        out.append(source.state, style=_SOURCE_STYLES.get(source.state, ""))
        if source.state in ("fault", "absent") and source.detail:
            out.append(f" ({source.detail[:70]})", style="dim")
    if poll_fault:
        out.append(f"\npoll failed: {poll_fault}", style="bold red")
    out.append(f"\nConfig     {snap.config_source}", style="dim")
    return out


def feed_line(entry: fv.FeedEntry) -> Text:
    out = Text(time.strftime("%H:%M", time.localtime(entry.ts)) + "  ", style="dim")
    out.append(f"{entry.epic[:24]:<25}", style="bold")
    out.append(f"{entry.subject[:14]:<15}")
    out.append(entry.text, style=_LEVEL_STYLES.get(entry.level, ""))
    return out


def dashboard_argv(workspace: Path) -> list[str]:
    """The command that opens an epic's dashboard: the installed ``bmad-loop`` on
    PATH, whose engines the factory runs, else this interpreter's own package."""
    exe = shutil.which("bmad-loop")
    head = [exe] if exe else [sys.executable, "-m", "bmad_loop"]
    return [*head, "tui", "--project", str(workspace)]


def open_dashboard(app, workspace: Path) -> str | None:
    """Run ``bmad-loop tui --project <workspace>`` in this terminal, this TUI
    suspended until it quits. Returns why it could not, or None."""
    argv = dashboard_argv(workspace)
    try:
        with app.suspend():
            subprocess.call(argv)
    except SuspendNotSupported:
        return f"cannot suspend here — run: {shlex.join(argv)}"
    except OSError as e:
        return f"could not start the epic's dashboard: {e}"
    return None


class FactoryScreen(ModalScreen[None]):
    """The factory overview. ``standalone``: the app's only screen (``bmad-loop
    tui --factory``), so there is no dashboard to go back to; ``origin`` is the
    project of the dashboard it was opened from, whose own row Enter returns to."""

    DEFAULT_CSS = """
    FactoryScreen {
        background: $background;
        layout: vertical;
    }
    FactoryScreen #factory-top {
        height: auto;
        max-height: 9;
    }
    FactoryScreen #factory-throughput, FactoryScreen #factory-capacity {
        width: 1fr;
        height: auto;
        padding: 0 1;
        border: round $primary-darken-2;
        border-title-color: $text;
        border-title-style: bold;
    }
    FactoryScreen #factory-epics {
        height: auto;
        max-height: 50%;
        min-height: 4;
        border-top: solid $primary-darken-2;
        border-title-color: $text;
        border-title-style: bold;
    }
    FactoryScreen #factory-strip {
        height: auto;
        padding: 0 1;
        background: $boost;
    }
    FactoryScreen #factory-feed {
        height: 1fr;
        min-height: 3;
        border-top: solid $primary-darken-2;
        border-title-color: $text;
        border-title-style: bold;
    }
    """

    BINDINGS = [
        Binding("o", "open_epic", "open epic"),
        Binding("t", "cycle_window", "window"),
        Binding("f", "filter_feed", "feed filter"),
        Binding("u", "refresh_now", "refresh"),
        Binding("escape", "close_factory", "back"),
        Binding("F", "close_factory", "back", show=False),
        Binding("q", "quit", "quit"),
    ]

    def __init__(
        self,
        config: fv.FactoryConfig,
        *,
        standalone: bool = False,
        origin: Path | None = None,
        reader: fv.FactoryReader | None = None,
    ):
        super().__init__()
        self.config = config
        self.standalone = standalone
        self.origin = origin
        self.reader = reader if reader is not None else fv.FactoryReader(config)
        self.snapshot: fv.FactorySnapshot | None = None
        self.window = 1  # index into factory_view.WINDOWS: 6h
        self.feed_filter: str | None = None  # an epic label
        self._rows: list[str] = []  # row keys, table order
        self._cells: dict[str, tuple[Text | str, ...]] = {}
        self._feed_shown: tuple[object, ...] | None = None
        self._poll_fault: str | None = None
        self._saved_subtitle = ""

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="factory-top"):
            throughput = Static(id="factory-throughput")
            throughput.border_title = "Throughput"
            yield throughput
            capacity = Static(id="factory-capacity")
            capacity.border_title = "Capacity and locks"
            yield capacity
        table = DataTable(id="factory-epics", cursor_type="row", zebra_stripes=True)
        table.border_title = "Epics"
        yield table
        yield Static(id="factory-strip")
        feed = RichLog(id="factory-feed", max_lines=_FEED_LINES, auto_scroll=False, wrap=False)
        feed.border_title = "Feed"
        yield feed
        yield Footer()

    def on_mount(self) -> None:
        self._saved_subtitle = self.app.sub_title
        self.app.sub_title = f"factory overview · {self.config.name}"
        table = self.query_one("#factory-epics", DataTable)
        for key, label, width in _COLUMNS:
            table.add_column(self._column_label(key, label), key=key, width=width)
        self.query_one("#factory-throughput", Static).update(Text("reading…", style="dim"))
        self._tick()
        self._slow()
        self.set_interval(self.config.refresh_s, self._tick)
        self.set_interval(_SLOW_TICK_S, self._slow)
        table.focus()

    def on_unmount(self) -> None:
        if not self.standalone:
            self.app.sub_title = self._saved_subtitle  # the dashboard's own, back with it

    def _column_label(self, key: str, label: str) -> str:
        if key == "done":
            return f"done {fv.WINDOWS[self.window][0]}"
        return label

    # ---------------------------------------------------------------- polling

    def _tick(self) -> None:
        self._poll()

    def _slow(self, force: bool = False) -> None:
        self._poll_slow(force)

    @work(thread=True, exclusive=True, group="factory-fast", exit_on_error=False)
    def _poll(self) -> None:
        try:
            snap = self.reader.poll()
        except Exception as e:  # an observer degrades: show the fault, keep the TUI
            self.app.call_from_thread(self._apply_fault, f"{type(e).__name__}: {e}")
            return
        if snap is not None:
            self.app.call_from_thread(self._apply, snap)

    @work(thread=True, exclusive=True, group="factory-slow", exit_on_error=False)
    def _poll_slow(self, force: bool) -> None:
        try:
            self.reader.poll_slow(force=force)
        except Exception as e:  # shown like a fast-path fault
            self.app.call_from_thread(self._apply_fault, f"{type(e).__name__}: {e}")

    def _apply_fault(self, fault: str) -> None:
        if not self.is_running:
            return
        self._poll_fault = fault
        if self.snapshot is not None:
            self.query_one("#factory-strip", Static).update(strip_text(self.snapshot, fault))
        else:
            self.query_one("#factory-strip", Static).update(
                Text(f"poll failed: {fault}", style="bold red")
            )

    def _apply(self, snap: fv.FactorySnapshot) -> None:
        # A worker's callback can land after the screen was dismissed.
        if not self.is_running:
            return
        self.snapshot = snap
        self._poll_fault = None
        self.query_one("#factory-throughput", Static).update(throughput_text(snap))
        self.query_one("#factory-capacity", Static).update(capacity_text(snap))
        self._apply_rows(snap)
        self.query_one("#factory-strip", Static).update(strip_text(snap, None))
        self._apply_feed(snap)

    def _apply_rows(self, snap: fv.FactorySnapshot) -> None:
        table = self.query_one("#factory-epics", DataTable)
        running = sum(1 for r in snap.rows if r.status == runs.RUNNING)
        table.border_title = (
            f"Epics · {len(snap.rows)} workspaces, {running} running"
            f" · done column: last {fv.WINDOWS[self.window][0]} (t)"
        )
        keys = [row.key for row in snap.rows]
        cells = {row.key: row_cells(row, snap.taken_at, self.window) for row in snap.rows}
        if keys != self._rows:
            cursor_key = (
                self._rows[table.cursor_row] if 0 <= table.cursor_row < len(self._rows) else None
            )
            table.clear()
            for key in keys:
                table.add_row(*cells[key], key=key)
            self._rows = keys
            if cursor_key in keys:
                table.move_cursor(row=keys.index(cursor_key))
        else:
            for key in keys:
                if self._cells.get(key) == cells[key]:
                    continue
                for (column, _label, _width), value in zip(_COLUMNS, cells[key]):
                    table.update_cell(key, column, value)
        self._cells = cells

    def _apply_feed(self, snap: fv.FactorySnapshot) -> None:
        entries = [e for e in snap.feed if self.feed_filter is None or e.epic == self.feed_filter]
        shown = (self.feed_filter, tuple(entries[:_FEED_LINES]))
        if shown == self._feed_shown:
            return
        self._feed_shown = shown
        feed = self.query_one("#factory-feed", RichLog)
        feed.border_title = (
            f"Feed · {self.feed_filter} (f: all epics)"
            if self.feed_filter
            else "Feed · all epics (f: this epic)"
        )
        feed.clear()
        for entry in entries[:_FEED_LINES]:
            feed.write(feed_line(entry))
        feed.scroll_home(animate=False)

    # ---------------------------------------------------------------- actions

    def highlighted(self) -> fv.EpicRow | None:
        if self.snapshot is None or not self._rows:
            return None
        table = self.query_one("#factory-epics", DataTable)
        if not 0 <= table.cursor_row < len(self._rows):
            return None
        key = self._rows[table.cursor_row]
        return next((r for r in self.snapshot.rows if r.key == key), None)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "factory-epics":
            self.action_open_epic()

    def action_open_epic(self) -> None:
        row = self.highlighted()
        if row is None:
            self.notify("no epic highlighted", severity="warning")
            return
        if not self.standalone and self.origin is not None and fv._real(self.origin) == row.key:
            self.dismiss()  # this dashboard's own epic: just go back to it
            return
        why = open_dashboard(self.app, row.workspace)
        if why is not None:
            self.notify(why, severity="warning", timeout=10)
        self._tick()

    def action_cycle_window(self) -> None:
        self.window = (self.window + 1) % len(fv.WINDOWS)
        table = self.query_one("#factory-epics", DataTable)
        column = table.columns.get("done")  # type: ignore[call-overload]
        if column is not None:
            column.label = Text(self._column_label("done", "done"))
            table.refresh()
        if self.snapshot is not None:
            self._cells = {}
            self._apply_rows(self.snapshot)

    def action_filter_feed(self) -> None:
        if self.feed_filter is not None:
            self.feed_filter = None
        else:
            row = self.highlighted()
            if row is None:
                return
            self.feed_filter = row.label
        if self.snapshot is not None:
            self._apply_feed(self.snapshot)

    def action_refresh_now(self) -> None:
        self.notify("refreshing ticket trees, TeamRouter and gh…", timeout=3)
        self._slow(force=True)
        self._tick()

    def action_close_factory(self) -> None:
        if not self.standalone:
            self.dismiss()

    def action_quit(self) -> None:
        self.app.exit()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "close_factory" and self.standalone:
            return False
        return True
