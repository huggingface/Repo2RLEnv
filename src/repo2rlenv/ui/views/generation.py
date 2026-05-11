"""Live view for synthesis pipelines (`repo2rlenv generate`).

Shows a header, a per-candidate progress bar, and a live tally of emitted
vs skipped tasks. Pipelines opt in by accepting a callable that fires when
each candidate is processed.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator, Literal

from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.text import Text

from repo2rlenv.ui.console import console as r2e_console, should_use_rich
from repo2rlenv.ui.live import live_view
from repo2rlenv.ui.theme import GLYPH, STYLE


CandidateOutcome = Literal["emit", "skip", "error"]


class GenerationView:
    """Live view for a synthesis pipeline run."""

    def __init__(
        self,
        *,
        repo: str,
        pipeline: str,
        model: str,
        limit: int,
        out: str,
    ):
        self.repo = repo
        self.pipeline = pipeline
        self.model = model
        self.limit = limit
        self.out = out

        self.emitted = 0
        self.skipped = 0
        self.errors = 0
        self.processed = 0
        self.skip_reasons: dict[str, int] = {}
        self.current: str = ""
        self.start_time = time.monotonic()

        self.outcome_panel: Panel | None = None
        self._live = None
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=None),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            expand=True,
        )
        self._progress_task = self._progress.add_task(
            description="Mining candidates", total=limit,
        )

    # ----- context manager ----------------------------------------------------

    def __enter__(self) -> "GenerationView":
        self._live_ctx = live_view(self._render())
        self._live = self._live_ctx.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        if hasattr(self, "_live_ctx"):
            self._live_ctx.__exit__(*exc)

    # ----- callbacks ----------------------------------------------------------

    def on_candidate(
        self,
        *,
        name: str,
        outcome: CandidateOutcome,
        reason: str = "",
    ) -> None:
        """Pipeline calls this once per candidate processed."""
        self.processed += 1
        self.current = name
        if outcome == "emit":
            self.emitted += 1
        elif outcome == "skip":
            self.skipped += 1
            if reason:
                self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1
        elif outcome == "error":
            self.errors += 1

        self._progress.update(self._progress_task, completed=self.processed,
                                description=f"Processing {name[:50]}")
        self._refresh()

    def set_outcome(self, *, emitted: int, skipped: int, skip_reasons: dict[str, int],
                     pushed: bool = False, registry_url: str = "") -> None:
        body = Text()
        body.append("emitted   ", style=STYLE.DIM)
        body.append(f"{emitted}\n", style="green")
        body.append("skipped   ", style=STYLE.DIM)
        body.append(f"{skipped}\n", style=STYLE.HIGHLIGHT if skipped else "white")
        if skip_reasons:
            body.append("reasons   ", style=STYLE.DIM)
            body.append(", ".join(f"{k}={v}" for k, v in skip_reasons.items()) + "\n")
        if registry_url:
            body.append("registry  ", style=STYLE.DIM)
            body.append(registry_url + "\n", style="bright_blue")
        body.append("out_dir   ", style=STYLE.DIM)
        body.append(self.out)
        self.outcome_panel = Panel(
            body,
            title=f"[{STYLE.SUCCESS}]{GLYPH.SUCCESS} Generation complete[/]",
            border_style=STYLE.PANEL_SUCCESS,
            expand=False,
        )
        self._refresh()

    # ----- rendering ----------------------------------------------------------

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render())

    def _render(self) -> RenderableType:
        parts: list[RenderableType] = [
            self._header_panel(),
            self._progress,
            self._stats_panel(),
        ]
        if self.outcome_panel is not None:
            parts.append(self.outcome_panel)
        return Group(*parts)

    def _header_panel(self) -> Panel:
        h = Text()
        h.append("r2e-generate ", style=STYLE.HEADER)
        h.append("· ", style=STYLE.DIM)
        h.append(self.repo, style="white")
        h.append(" · ", style=STYLE.DIM)
        h.append(self.pipeline, style="bright_blue")
        h.append(" · ", style=STYLE.DIM)
        h.append(self.model, style="bright_blue")
        meta = Text()
        meta.append("limit: ", style=STYLE.DIM)
        meta.append(str(self.limit), style="white")
        meta.append("   out: ", style=STYLE.DIM)
        meta.append(self.out, style="white")
        return Panel(Group(h, meta), border_style=STYLE.PANEL_INFO, expand=True)

    def _stats_panel(self) -> Panel:
        line = Text()
        line.append(f"{GLYPH.SUCCESS} emitted ", style=STYLE.SUCCESS)
        line.append(f"{self.emitted}", style="white")
        line.append("   ", style=STYLE.DIM)
        line.append(f"{GLYPH.PENDING} skipped ", style=STYLE.WARN)
        line.append(f"{self.skipped}", style="white")
        if self.errors:
            line.append("   ", style=STYLE.DIM)
            line.append(f"{GLYPH.ERROR} errors ", style=STYLE.ERROR)
            line.append(f"{self.errors}", style="white")
        elapsed = time.monotonic() - self.start_time
        line.append("   ·   elapsed ", style=STYLE.DIM)
        line.append(f"{elapsed:.1f}s", style="white")

        if self.skip_reasons:
            reasons = Text()
            reasons.append("skip reasons: ", style=STYLE.DIM)
            reasons.append(", ".join(f"{k}={v}" for k, v in self.skip_reasons.items()),
                            style=STYLE.DIM)
            body: RenderableType = Group(line, reasons)
        else:
            body = line
        return Panel(body, border_style=STYLE.PANEL_DIM, expand=True)


@contextmanager
def generation_view_or_plain(
    *,
    repo: str,
    pipeline: str,
    model: str,
    limit: int,
    out: str,
    force_plain: bool = False,
) -> Iterator["GenerationView | None"]:
    if force_plain or not should_use_rich():
        yield None
        return
    with GenerationView(repo=repo, pipeline=pipeline, model=model,
                          limit=limit, out=out) as view:
        yield view
