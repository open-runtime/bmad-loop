"""`bmad-loop tui --factory`: the factory overview on its own.

A small app around :class:`~bmad_loop.tui.screens.factory.FactoryScreen` with no
run-control bindings at all: it only reads. Run control stays in each epic's
own dashboard, which Enter on a row opens (the overview suspends until it quits).
"""

from __future__ import annotations

from collections.abc import Iterable

from textual.app import App, SystemCommand
from textual.screen import Screen

from ..factory_view import FactoryConfig, FactoryReader
from . import commands
from .screens.factory import FactoryScreen


class FactoryApp(App[None]):
    TITLE = "bmad-loop"

    def __init__(self, config: FactoryConfig, *, reader: FactoryReader | None = None):
        super().__init__()
        self.config = config
        self.reader = reader  # a seam for tests; None reads with a FactoryReader of config
        self.sub_title = f"factory overview · {config.name}"

    def on_mount(self) -> None:
        self.push_screen(FactoryScreen(self.config, standalone=True, reader=self.reader))

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        yield from commands.palette_commands(screen, self.run_action)


def run_factory_tui(config: FactoryConfig) -> int:
    FactoryApp(config).run()
    return 0
