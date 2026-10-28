"""Pilot tests for the factory overview (tui/screens/factory.py, tui/factory_app.py)
and its `bmad-loop tui --factory` entry. The read model's numbers are pinned in
test_factory_view.py; here we prove the wiring: the overview mounts and fills,
opens over the dashboard with `F` as a modal that blocks the run-control keys,
opens an epic's dashboard on Enter, and survives a poll that raises."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from test_factory_view import build_factory
from test_tui_app import until
from textual.coordinate import Coordinate
from textual.widgets import DataTable, RichLog, Static

from bmad_loop import cli
from bmad_loop import factory_view as fv
from bmad_loop.tui import commands
from bmad_loop.tui.app import BmadLoopApp
from bmad_loop.tui.factory_app import FactoryApp
from bmad_loop.tui.screens import factory as factory_screen
from bmad_loop.tui.screens.dashboard import DashboardScreen
from bmad_loop.tui.screens.factory import FactoryScreen

SIZE = (180, 50)


def _cells(screen: FactoryScreen, column: int) -> list[str]:
    table = screen.query_one("#factory-epics", DataTable)
    return [str(table.get_cell_at(Coordinate(row, column))) for row in range(table.row_count)]


def _text(screen: FactoryScreen, selector: str) -> str:
    return str(screen.query_one(selector, Static).content)


def _filled(app) -> bool:
    screen = app.screen
    return (
        isinstance(screen, FactoryScreen)
        and screen.snapshot is not None
        and screen.query_one("#factory-epics", DataTable).row_count == 3
    )


async def test_factory_app_shows_every_epic_and_the_throughput(tmp_path: Path):
    f = build_factory(tmp_path, time.time())
    app = FactoryApp(f.config)
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: _filled(app), what="three epic rows")
        screen = app.screen
        assert isinstance(screen, FactoryScreen) and screen.standalone
        assert _cells(screen, 2) == ["alpha", "beta", "gamma"]
        assert _cells(screen, 5) == ["1.3", "2.1", "—"]
        assert _cells(screen, 10)[0] == "holds build 10m"
        throughput = _text(screen, "#factory-throughput")
        assert "stories done" in throughput and "1h 1 (1.0/h)" in throughput
        assert "cycle median  2h00m over 2 stories" in throughput
        capacity = _text(screen, "#factory-capacity")
        assert "1 running of 4" in capacity and "build" in capacity
        strip = _text(screen, "#factory-strip")
        assert "roster 5m old" in strip and "Config     test" in strip
        feed = screen.query_one("#factory-feed", RichLog)
        assert "session started (claude·opus)" in str(feed.lines[0].text)
        assert app.sub_title == "factory overview · test-factory"
        # standalone: there is no dashboard behind it, so escape does nothing
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, FactoryScreen)
        await pilot.press("q")
        await until(pilot, lambda: not app.is_running, what="q quits")


async def test_enter_opens_the_highlighted_epics_dashboard(tmp_path: Path, monkeypatch):
    f = build_factory(tmp_path, time.time())
    opened: list[Path] = []
    monkeypatch.setattr(factory_screen, "open_dashboard", lambda app, ws: opened.append(ws))
    app = FactoryApp(f.config)
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: _filled(app), what="three epic rows")
        table = app.screen.query_one("#factory-epics", DataTable)
        table.move_cursor(row=1)
        await pilot.press("enter")
        await until(pilot, lambda: opened == [f.beta], what="beta's dashboard opened")
        table.move_cursor(row=2)
        await pilot.press("o")
        await until(pilot, lambda: opened == [f.beta, f.gamma], what="o opens gamma's")


async def test_window_and_feed_filter_keys(tmp_path: Path):
    f = build_factory(tmp_path, time.time())
    app = FactoryApp(f.config)
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: _filled(app), what="three epic rows")
        screen = app.screen
        assert isinstance(screen, FactoryScreen)
        table = screen.query_one("#factory-epics", DataTable)
        assert str(table.columns["done"].label) == "done 6h"  # type: ignore[index]
        assert _cells(screen, 4)[0] == "2"
        await pilot.press("t")  # 24h
        await pilot.press("t")  # all
        await until(pilot, lambda: _cells(screen, 4)[0] == "3", what="all-time done count")
        assert str(table.columns["done"].label) == "done all"  # type: ignore[index]
        table.move_cursor(row=1)
        await pilot.press("f")
        feed = screen.query_one("#factory-feed", RichLog)
        await until(pilot, lambda: screen.feed_filter == "beta", what="feed filtered to beta")
        assert feed.lines and all(" beta " in f" {line.text} " for line in feed.lines)
        await pilot.press("f")
        await until(pilot, lambda: screen.feed_filter is None, what="feed back to all epics")


def _project_config(f) -> None:
    (f.alpha / fv.FACTORY_CONFIG_REL).write_text(
        "[factory]\n"
        'name = "from-project"\n'
        f'workspaces_root = "{f.root}"\n'
        f'scheduler_state = "{f.scheduler_state}"\n'
        f'locks_dir = "{f.locks}"\n'
        "tickets = false\n",
        encoding="utf-8",
    )


async def test_dashboard_F_opens_the_overview_as_a_modal_that_blocks_run_control(tmp_path: Path):
    f = build_factory(tmp_path, time.time())
    _project_config(f)
    app = BmadLoopApp(f.alpha)
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen), what="dashboard")
        await pilot.press("F")
        await until(pilot, lambda: _filled(app), what="the overview over the dashboard")
        screen = app.screen
        assert isinstance(screen, FactoryScreen) and not screen.standalone
        assert screen.config.name == "from-project"
        # The dashboard's run-control keys would act on the dashboard's own (live)
        # run; with the overview on top none of them may reach the app.
        for key in ("x", "S", "D", "A", "r", "s", "e", "R", "V", "a", "c", "v", "g"):
            await pilot.press(key)
            await pilot.pause()
            assert app.screen is screen, f"{key} reached the app behind the overview"
        titles = {c.title for c in app.get_system_commands(app.screen)}
        assert commands.PALETTE["open_epic"][0] in titles
        assert commands.PALETTE["close_factory"][0] in titles
        assert commands.PALETTE["stop_run"][0] not in titles
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen), what="back")
        assert app.sub_title == str(app.project)


async def test_enter_on_the_dashboards_own_epic_goes_back_to_it(tmp_path: Path, monkeypatch):
    f = build_factory(tmp_path, time.time())
    _project_config(f)
    opened: list[Path] = []
    monkeypatch.setattr(factory_screen, "open_dashboard", lambda app, ws: opened.append(ws))
    app = BmadLoopApp(f.alpha)
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen), what="dashboard")
        await pilot.press("F")
        await until(pilot, lambda: _filled(app), what="the overview")
        app.screen.query_one("#factory-epics", DataTable).move_cursor(row=0)  # alpha: this project
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen), what="back to alpha")
        assert opened == []


async def test_a_broken_project_config_is_a_toast_not_a_crash(tmp_path: Path):
    f = build_factory(tmp_path, time.time())
    (f.alpha / fv.FACTORY_CONFIG_REL).write_text("[factory]\n", encoding="utf-8")
    app = BmadLoopApp(f.alpha)
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen), what="dashboard")
        await pilot.press("F")
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        assert any("workspaces_root is required" in str(n.message) for n in app._notifications)


class _BrokenReader(fv.FactoryReader):
    def poll(self):
        raise RuntimeError("disk on fire")


async def test_a_poll_that_raises_is_shown_and_the_overview_stays_up(tmp_path: Path):
    f = build_factory(tmp_path, time.time())
    app = FactoryApp(f.config, reader=_BrokenReader(f.config))
    async with app.run_test(size=SIZE) as pilot:
        await until(
            pilot,
            lambda: isinstance(app.screen, FactoryScreen)
            and "poll failed: RuntimeError: disk on fire" in _text(app.screen, "#factory-strip"),
            what="the poll fault on the strip",
        )
        assert app.is_running


# ---------------------------------------------------------------- the CLI


@pytest.fixture
def captured(monkeypatch) -> list[fv.FactoryConfig]:
    import bmad_loop.tui.factory_app as factory_app

    seen: list[fv.FactoryConfig] = []
    monkeypatch.setattr(factory_app, "run_factory_tui", lambda config: seen.append(config) or 0)
    return seen


def test_tui_factory_flag_opens_the_named_config(tmp_path: Path, captured):
    config = tmp_path / "f.toml"
    config.write_text("[factory]\nname = 'named'\nworkspaces_root = 'w'\n", encoding="utf-8")
    assert cli.main(["tui", "--project", str(tmp_path), "--factory", str(config)]) == 0
    assert [c.name for c in captured] == ["named"]
    assert captured[0].workspaces_root == tmp_path / "w"


def test_tui_factory_flag_alone_reads_the_project_then_the_default(tmp_path: Path, captured):
    assert cli.main(["tui", "--project", str(tmp_path), "--factory"]) == 0
    assert captured[-1].source == fv.DEFAULT_CONFIG_SOURCE
    (tmp_path / ".bmad-loop").mkdir()
    (tmp_path / fv.FACTORY_CONFIG_REL).write_text(
        "[factory]\nname = 'local'\nworkspaces_root = 'w'\n", encoding="utf-8"
    )
    assert cli.main(["tui", "--project", str(tmp_path), "--factory"]) == 0
    assert captured[-1].name == "local"


def test_tui_factory_flag_with_a_bad_config_fails_before_the_tui(tmp_path: Path, captured, capsys):
    assert (
        cli.main(["tui", "--project", str(tmp_path), "--factory", str(tmp_path / "nope.toml")]) == 1
    )
    assert "cannot read" in capsys.readouterr().err
    assert captured == []


def test_tui_without_the_factory_flag_still_opens_the_dashboard(
    tmp_path: Path, captured, monkeypatch
):
    import bmad_loop.tui.app as tui_app

    opened: list[Path] = []
    monkeypatch.setattr(tui_app, "run_tui", lambda project: opened.append(project) or 0)
    assert cli.main(["tui", "--project", str(tmp_path)]) == 0
    assert opened == [tmp_path.resolve()] and captured == []
