"""Rich-based live UI for the bootstrap CLI — Modal-style.

Drives a `Live` display that re-renders each time `on_turn` is called by the
agent loop. Falls back to plain logging in non-TTY environments (CI, piped
output, etc.) — see `should_use_rich()`.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from typing import Iterator

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from repo2rlenv.bootstrap.agent import AgentTurn


_ACTION_STYLE = {
    "BASH":       "bright_green",
    "READ_FILE":  "cyan",
    "LIST_DIR":   "cyan",
    "SAVE_SETUP": "bold green",
    "GIVE_UP":    "bold red",
    "INVALID":    "yellow",
}


def should_use_rich() -> bool:
    """Use Rich UI only on a real terminal. Respect NO_COLOR / dumb terminals."""
    if os.environ.get("NO_COLOR") or os.environ.get("CI"):
        return False
    if not sys.stdout.isatty():
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return True


class RichBootstrapUI:
    """Live, redrawing display of an in-flight bootstrap agent loop.

    Use as a context manager:
        with RichBootstrapUI(...) as ui:
            ensure_bootstrap(..., on_turn=ui.on_turn)
            ui.set_outcome(result)
    """

    def __init__(
        self,
        *,
        console: Console | None = None,
        repo: str,
        ref: str,
        model: str,
        max_iterations: int,
        language: str,
        base_image: str,
    ):
        self.console = console or Console()
        self.repo = repo
        self.ref = ref
        self.model = model
        self.max_iterations = max_iterations
        self.language = language
        self.base_image = base_image

        self.turns: list[AgentTurn] = []
        self.total_cost = 0.0
        self.total_tokens_in = 0
        self.total_tokens_out = 0
        self.start_time = time.monotonic()
        self.outcome_panel: Panel | None = None

        self._live: Live | None = None

    # ----- context manager ----------------------------------------------------

    def __enter__(self) -> "RichBootstrapUI":
        self._live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=4,
            transient=False,
        )
        self._live.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        if self._live is not None:
            # Render one last time with the final outcome panel
            self._live.update(self._render())
            self._live.__exit__(*exc)

    # ----- public callbacks ---------------------------------------------------

    def on_turn(self, turn: AgentTurn, total_cost: float) -> None:
        """Hand this to `ensure_bootstrap(..., on_turn=ui.on_turn)`."""
        self.turns.append(turn)
        self.total_cost = total_cost
        self.total_tokens_in += turn.prompt_tokens
        self.total_tokens_out += turn.completion_tokens
        if self._live is not None:
            self._live.update(self._render())

    def set_outcome(
        self,
        *,
        success: bool,
        image_digest: str = "",
        image_tag: str = "",
        rebuild_cmds: list[str] | None = None,
        test_cmds: list[str] | None = None,
        reason: str = "",
    ) -> None:
        """Call once at the end to add the success/failure panel."""
        if success:
            body = Text()
            body.append("image_digest  ", style="dim")
            body.append(f"{image_digest}\n", style="green")
            body.append("image_tag     ", style="dim")
            body.append(f"{image_tag}\n", style="green")
            body.append("rebuild_cmds  ", style="dim")
            body.append("; ".join(rebuild_cmds or []) + "\n")
            body.append("test_cmds     ", style="dim")
            body.append("; ".join(test_cmds or []))
            self.outcome_panel = Panel(
                body,
                title="[bold green]✓ Bootstrap succeeded[/bold green]",
                border_style="green",
            )
        else:
            self.outcome_panel = Panel(
                Text(reason, style="red"),
                title="[bold red]✗ Bootstrap failed[/bold red]",
                border_style="red",
            )
        if self._live is not None:
            self._live.update(self._render())

    # ----- rendering ----------------------------------------------------------

    def _render(self) -> RenderableType:
        header = self._header_panel()
        table = self._steps_table()
        stats = self._stats_line()
        parts = [header, table, stats]
        if self.outcome_panel is not None:
            parts.append(self.outcome_panel)
        return Group(*parts)

    def _header_panel(self) -> Panel:
        h = Text()
        h.append("r2e-bootstrap ", style="bold cyan")
        h.append("· ", style="dim")
        h.append(f"{self.repo}@{self.ref[:12]} ", style="white")
        h.append("· ", style="dim")
        h.append(self.model, style="bright_blue")
        meta = Text()
        meta.append(f"language: ", style="dim")
        meta.append(self.language, style="white")
        meta.append("  base: ", style="dim")
        meta.append(self.base_image, style="white")
        meta.append("  max iter: ", style="dim")
        meta.append(str(self.max_iterations), style="white")
        return Panel(Group(h, meta), border_style="cyan", expand=False)

    def _steps_table(self) -> Table:
        table = Table(
            show_header=True,
            header_style="bold magenta",
            show_edge=False,
            pad_edge=False,
            box=None,
        )
        table.add_column("#", width=4, justify="right", style="dim")
        table.add_column("Action", width=11)
        table.add_column("Input", overflow="ellipsis", max_width=70)
        table.add_column("Duration", width=8, justify="right", style="dim")
        table.add_column("Cost", width=10, justify="right", style="dim")

        for t in self.turns:
            style = _ACTION_STYLE.get(t.action.name, "white")
            inp = t.action.input.replace("\n", " ⏎ ")
            table.add_row(
                str(t.step + 1),
                Text(t.action.name, style=style),
                inp,
                f"{t.duration_sec:.1f}s" if t.duration_sec else "—",
                f"${t.cost_estimate_usd:.4f}" if t.cost_estimate_usd else "—",
            )
        return table

    def _stats_line(self) -> Text:
        elapsed = time.monotonic() - self.start_time
        line = Text()
        line.append(f"  Iterations ", style="dim")
        line.append(f"{len(self.turns)}/{self.max_iterations}", style="white")
        line.append("  ·  ", style="dim")
        line.append("Tokens ", style="dim")
        line.append(f"{self.total_tokens_in/1000:.1f}K", style="white")
        line.append(" in / ", style="dim")
        line.append(f"{self.total_tokens_out/1000:.1f}K", style="white")
        line.append(" out  ·  ", style="dim")
        line.append("Cost ≈ ", style="dim")
        line.append(f"${self.total_cost:.4f}", style="bright_yellow" if self.total_cost > 0 else "dim")
        line.append("  ·  ", style="dim")
        line.append("Elapsed ", style="dim")
        line.append(f"{elapsed:.1f}s", style="white")
        return line


@contextmanager
def bootstrap_ui(
    *,
    repo: str,
    ref: str,
    model: str,
    max_iterations: int,
    language: str,
    base_image: str,
    force_plain: bool = False,
) -> Iterator["RichBootstrapUI | None"]:
    """Context manager that yields a UI when appropriate, else None.

    Caller writes:
        with bootstrap_ui(...) as ui:
            on_turn = ui.on_turn if ui else None
            result = ensure_bootstrap(..., on_turn=on_turn)
            if ui: ui.set_outcome(...)
    """
    if force_plain or not should_use_rich():
        yield None
        return
    ui = RichBootstrapUI(
        repo=repo,
        ref=ref,
        model=model,
        max_iterations=max_iterations,
        language=language,
        base_image=base_image,
    )
    with ui:
        yield ui
