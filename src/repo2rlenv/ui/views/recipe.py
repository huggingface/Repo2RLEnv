"""One recipe-aware view, rendered from the same events used by plain/JSON logs."""

from __future__ import annotations

from contextlib import contextmanager

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from repo2rlenv.campaigns.events import EventJournal, ProgressEvent
from repo2rlenv.ui import console
from repo2rlenv.ui.console import should_use_rich
from repo2rlenv.ui.live import live_view


class RecipeView:
    def __init__(self, recipe: str, source: str, target: int, provider: str):
        self.recipe, self.source, self.target, self.provider = recipe, source, target, provider
        self.stage = "preflight"
        self.state = "started"
        self.task = ""
        self.message = ""
        self.metrics: dict[str, int | float | str] = {"attempted": 0, "exported": 0, "accepted": 0}

    def update(self, event: ProgressEvent) -> None:
        self.stage, self.state = event.stage, event.state
        self.task, self.message = event.task_id or "", event.message
        self.metrics.update(event.metrics)

    def render(self):
        header = Text(
            f"{self.recipe} · {self.provider}\n{self.source}\nCandidate target: {self.target}"
        )
        status = Text(f"{self.stage} · {self.state}\n{self.task}\n{self.message}")
        table = Table.grid(padding=(0, 2))
        for key, value in self.metrics.items():
            table.add_row(Text(key.replace("_", " ")), Text(str(value)))
        return Group(
            Panel(header, title="Repo2RLEnv"),
            Panel(status, title="Current stage"),
            Panel(table, title="Progress and usage"),
        )


@contextmanager
def recipe_events(
    view: RecipeView, journal: EventJournal, *, plain: bool = False, json_output: bool = False
):
    def record(event: ProgressEvent, live=None):
        journal.emit(event)
        view.update(event)
        if json_output:
            console.json(event.model_dump())
        elif live is not None:
            live.update(view.render())
        else:
            console.print(Text(f"[{event.recipe}/{event.stage}] {event.state}: {event.message}"))

    if plain or json_output or not should_use_rich():
        yield record
    else:
        with live_view(view.render()) as live:
            yield lambda event: record(event, live)
