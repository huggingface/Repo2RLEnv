"""Bootstrap live view — built on shared UI primitives.

Six panels stacked top-to-bottom:

    Header    — repo · model · language · base · max iter
    Phases    — clone / pull / sandbox / agent / commit / push status
    Steps     — completed agent turns with action + duration + cost
    Now       — spinner + "thinking..." or "executing X → cmd"
    Thought   — latest LLM reasoning
    Output    — latest tool observation (last ~14 lines)
    Stats     — iter · tokens · cost · elapsed
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from repo2rlenv.bootstrap.agent import AgentAction, AgentTurn
from repo2rlenv.ui.console import console as r2e_console, should_use_rich
from repo2rlenv.ui.live import live_view
from repo2rlenv.ui.primitives import error_panel, success_panel
from repo2rlenv.ui.theme import ACTION_STYLE_MAP, GLYPH, STYLE


_PHASES = ["clone", "pull", "sandbox", "agent", "commit", "push"]
_PHASE_GLYPH = {
    "clone":   GLYPH.PHASE_CLONE,
    "pull":    GLYPH.PHASE_PULL,
    "sandbox": GLYPH.PHASE_SANDBOX,
    "agent":   GLYPH.PHASE_AGENT,
    "commit":  GLYPH.PHASE_COMMIT,
    "push":    GLYPH.PHASE_PUSH,
}


@dataclass
class PhaseState:
    name: str
    status: str = "pending"   # pending | running | done | skipped | failed
    detail: str = ""
    started_at: float = 0.0
    duration_sec: float = 0.0


class BootstrapView:
    """Live, redrawing view of the bootstrap agent loop. Use as a context manager."""

    def __init__(
        self,
        *,
        repo: str,
        ref: str,
        model: str,
        max_iterations: int,
        language: str,
        base_image: str,
    ):
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

        self.status: str = "starting"
        self.current_action: AgentAction | None = None
        self.outcome_panel: Panel | None = None

        self.phases: dict[str, PhaseState] = {p: PhaseState(name=p) for p in _PHASES}

        self._spinner = Spinner("dots", text="", style=STYLE.HIGHLIGHT)
        self._live = None

    # ----- context manager ----------------------------------------------------

    def __enter__(self) -> "BootstrapView":
        self._live_ctx = live_view(self._render())
        self._live = self._live_ctx.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        if self._live is not None:
            self.status = "done"
            self._live.update(self._render())
        if hasattr(self, "_live_ctx"):
            self._live_ctx.__exit__(*exc)

    # ----- runner callbacks ---------------------------------------------------

    def on_phase(self, phase: str, details: dict) -> None:
        """Receive runner phase notifications."""
        # Special "detected" event: update header language/base
        if phase == "detected":
            if "language" in details:
                self.language = details["language"]
            if "base_image" in details:
                self.base_image = details["base_image"]
            self._refresh()
            return
        # Phase events: "<phase>_start|_progress|_done|_skipped|_failed"
        for name in _PHASES:
            if phase == f"{name}_start":
                self.phases[name].status = "running"
                self.phases[name].started_at = time.monotonic()
                self.phases[name].detail = details.get("detail", "")
                break
            if phase == f"{name}_progress":
                # Live update detail without changing status — keeps the spinner spinning
                self.phases[name].status = "running"
                if not self.phases[name].started_at:
                    self.phases[name].started_at = time.monotonic()
                self.phases[name].detail = details.get("detail", self.phases[name].detail)
                break
            if phase == f"{name}_done":
                self.phases[name].status = "done"
                if self.phases[name].started_at:
                    self.phases[name].duration_sec = time.monotonic() - self.phases[name].started_at
                self.phases[name].detail = details.get("detail", self.phases[name].detail)
                break
            if phase == f"{name}_skipped":
                self.phases[name].status = "skipped"
                self.phases[name].detail = details.get("detail", "")
                break
            if phase == f"{name}_failed":
                self.phases[name].status = "failed"
                self.phases[name].detail = details.get("detail", "")
                break
        self._refresh()

    def on_thinking(self, step: int) -> None:
        self.status = "thinking"
        self.current_action = None
        self.phases["agent"].status = "running"
        self._refresh()

    def on_executing(self, step: int, action: AgentAction) -> None:
        self.status = "executing"
        self.current_action = action
        self._refresh()

    def on_turn(self, turn: AgentTurn, total_cost: float) -> None:
        self.turns.append(turn)
        self.total_cost = total_cost
        self.total_tokens_in += turn.prompt_tokens
        self.total_tokens_out += turn.completion_tokens
        self.status = "thinking"
        self.current_action = None
        self._refresh()

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
        if success:
            self.phases["agent"].status = "done"
            body = Text()
            body.append("image_digest  ", style=STYLE.DIM)
            body.append(f"{image_digest}\n", style="green")
            body.append("image_tag     ", style=STYLE.DIM)
            body.append(f"{image_tag}\n", style="green")
            if rebuild_cmds:
                body.append("rebuild_cmds  ", style=STYLE.DIM)
                body.append(" && ".join(rebuild_cmds) + "\n")
            if test_cmds:
                body.append("test_cmds     ", style=STYLE.DIM)
                body.append(" && ".join(test_cmds))
            self.outcome_panel = success_panel(body, title="Bootstrap succeeded")
        else:
            self.outcome_panel = error_panel(Text(reason, style="red"),
                                              title="Bootstrap failed")
            for p in self.phases.values():
                if p.status == "running":
                    p.status = "failed"
        self.status = "done"
        self._refresh()

    # ----- rendering ----------------------------------------------------------

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render())

    def _render(self) -> RenderableType:
        parts: list[RenderableType] = [
            self._header_panel(),
            self._phases_panel(),
            self._steps_panel(),
            self._current_step_panel(),
            self._latest_thought_panel(),
            self._latest_output_panel(),
            self._stats_line(),
        ]
        if self.outcome_panel is not None:
            parts.append(self.outcome_panel)
        return Group(*parts)

    def _header_panel(self) -> Panel:
        h = Text()
        h.append("r2e-bootstrap ", style=STYLE.HEADER)
        h.append("· ", style=STYLE.DIM)
        h.append(f"{self.repo}@{self.ref[:12]} ", style="white")
        h.append("· ", style=STYLE.DIM)
        h.append(self.model, style="bright_blue")
        meta = Text()
        meta.append("language: ", style=STYLE.DIM)
        meta.append(self.language, style="white")
        meta.append("   base: ", style=STYLE.DIM)
        meta.append(self.base_image, style="white")
        meta.append("   max iter: ", style=STYLE.DIM)
        meta.append(str(self.max_iterations), style="white")
        return Panel(Group(h, meta), border_style=STYLE.PANEL_INFO, expand=True)

    def _phases_panel(self) -> Panel:
        table = Table(show_header=False, show_edge=False, pad_edge=False, box=None,
                       expand=True)
        table.add_column("", width=2)        # icon
        table.add_column("", width=3)        # phase emoji
        table.add_column("Phase", width=10)
        table.add_column("Detail")
        table.add_column("Time", justify="right", width=8, style=STYLE.DIM)

        for name in _PHASES:
            p = self.phases[name]
            if p.status == "done":
                icon = Text(GLYPH.SUCCESS, style=STYLE.SUCCESS)
            elif p.status == "running":
                icon = self._spinner
            elif p.status == "failed":
                icon = Text(GLYPH.ERROR, style=STYLE.ERROR)
            elif p.status == "skipped":
                icon = Text(GLYPH.PENDING, style=STYLE.MUTED)
            else:
                icon = Text(GLYPH.PENDING, style=STYLE.MUTED)

            label_style = STYLE.MUTED if p.status in ("pending", "skipped") else "white"
            time_str = f"{p.duration_sec:.1f}s" if p.duration_sec else ""
            detail = p.detail or ("(skipped)" if p.status == "skipped" else
                                  "(pending)" if p.status == "pending" else "")
            table.add_row(
                icon,
                _PHASE_GLYPH[name],
                Text(name, style=label_style),
                Text(detail, style=STYLE.DIM if p.status in ("pending", "skipped") else "white"),
                time_str,
            )
        return Panel(table, title="[bold]Phases[/bold]", border_style=STYLE.PANEL_DIM, expand=True)

    def _status_icon(self, action_name: str) -> Text:
        if action_name == "SAVE_SETUP":
            return Text(GLYPH.SUCCESS, style=STYLE.SUCCESS)
        if action_name == "GIVE_UP":
            return Text(GLYPH.ERROR, style=STYLE.ERROR)
        if action_name == "INVALID":
            return Text(GLYPH.WARN, style=STYLE.WARN)
        return Text(GLYPH.SUCCESS, style="green")

    def _steps_panel(self) -> Panel:
        table = Table(show_header=True, header_style="bold magenta",
                       show_edge=False, pad_edge=False, box=None, expand=True)
        table.add_column("", width=2)
        table.add_column("#", width=3, justify="right", style=STYLE.DIM)
        table.add_column("Action", width=11)
        table.add_column("Input", overflow="ellipsis", no_wrap=True, max_width=80)
        table.add_column("Time", width=7, justify="right", style=STYLE.DIM)
        table.add_column("Cost", width=9, justify="right", style=STYLE.DIM)

        if not self.turns:
            table.add_row(
                Text(GLYPH.PENDING, style=STYLE.DIM), "", Text("...", style=STYLE.DIM),
                Text("waiting for first agent response", style=f"{STYLE.DIM} italic"),
                "", "",
            )
        for t in self.turns:
            style = ACTION_STYLE_MAP.get(t.action.name, "white")
            inp = t.action.input.replace("\n", " ⏎ ")
            table.add_row(
                self._status_icon(t.action.name),
                str(t.step + 1),
                Text(t.action.name, style=style),
                inp,
                f"{t.duration_sec:.1f}s" if t.duration_sec else "—",
                f"${t.cost_estimate_usd:.4f}" if t.cost_estimate_usd else "—",
            )
        return Panel(table, title="[bold]Steps[/bold]", border_style=STYLE.PANEL_DIM, expand=True)

    def _current_step_panel(self) -> Panel:
        step_num = len(self.turns) + 1
        if self.status == "thinking":
            label = Text()
            label.append(f"Step {step_num}: ", style=STYLE.DIM)
            label.append("thinking...", style=STYLE.HIGHLIGHT)
            renderable: RenderableType = Group(self._spinner, label)
        elif self.status == "executing" and self.current_action is not None:
            label = Text()
            label.append(f"Step {step_num}: ", style=STYLE.DIM)
            label.append("executing ", style=STYLE.HIGHLIGHT)
            label.append(self.current_action.name,
                          style=ACTION_STYLE_MAP.get(self.current_action.name, "white"))
            label.append(" → ", style=STYLE.DIM)
            label.append(self.current_action.input[:120].replace("\n", " ⏎ "))
            renderable = Group(self._spinner, label)
        elif self.status == "done":
            renderable = Text("done", style="green")
        else:
            renderable = Text("idle", style=STYLE.DIM)
        return Panel(renderable, title="[bold]Now[/bold]", border_style="yellow", expand=True)

    def _latest_thought_panel(self) -> Panel:
        if not self.turns:
            text = Text("(none yet)", style=f"{STYLE.DIM} italic")
        else:
            text = Text(self.turns[-1].thought or "(no thought)", style="white")
        return Panel(text, title="[bold]Latest thought[/bold]",
                       border_style=STYLE.PANEL_DIM, expand=True)

    def _latest_output_panel(self) -> Panel:
        if not self.turns:
            body: RenderableType = Text("(no tool output yet)", style=f"{STYLE.DIM} italic")
        else:
            obs = self.turns[-1].observation or "(empty)"
            obs = _dedupe_consecutive(obs)
            lines = obs.splitlines()
            if len(lines) > 14:
                hidden = len(lines) - 14
                tail = "\n".join(lines[-14:])
                obs = f"... [{hidden} earlier lines elided] ...\n{tail}"
            body = Text(obs, style="white", no_wrap=False)
        return Panel(body, title="[bold]Latest output[/bold]",
                       border_style=STYLE.PANEL_DIM, expand=True)

    def _stats_line(self) -> Padding:
        elapsed = time.monotonic() - self.start_time
        line = Text()
        line.append("  iter ", style=STYLE.DIM)
        line.append(f"{len(self.turns)}/{self.max_iterations}", style="white")
        line.append("  ·  ", style=STYLE.DIM)
        line.append("tokens ", style=STYLE.DIM)
        line.append(f"{self.total_tokens_in / 1000:.1f}K", style="white")
        line.append(" in / ", style=STYLE.DIM)
        line.append(f"{self.total_tokens_out / 1000:.1f}K", style="white")
        line.append(" out  ·  ", style=STYLE.DIM)
        line.append("cost ≈ ", style=STYLE.DIM)
        line.append(f"${self.total_cost:.4f}",
                     style=STYLE.HIGHLIGHT if self.total_cost > 0 else STYLE.DIM)
        line.append("  ·  ", style=STYLE.DIM)
        line.append("elapsed ", style=STYLE.DIM)
        line.append(f"{elapsed:.1f}s", style="white")
        return Padding(line, (0, 0))


def _dedupe_consecutive(text: str) -> str:
    """Collapse runs of identical lines into '<line> (×N)'."""
    out = []
    last = None
    count = 0
    for line in text.splitlines():
        if line == last:
            count += 1
        else:
            if last is not None and count > 1:
                out[-1] = f"{last} (×{count})"
            out.append(line)
            last = line
            count = 1
    if last is not None and count > 1:
        out[-1] = f"{last} (×{count})"
    return "\n".join(out)


@contextmanager
def bootstrap_view_or_plain(
    *,
    repo: str,
    ref: str,
    model: str,
    max_iterations: int,
    language: str,
    base_image: str,
    force_plain: bool = False,
) -> Iterator["BootstrapView | None"]:
    """Yield a BootstrapView if Rich is appropriate, else None (plain mode)."""
    if force_plain or not should_use_rich():
        yield None
        return
    with BootstrapView(
        repo=repo, ref=ref, model=model,
        max_iterations=max_iterations,
        language=language, base_image=base_image,
    ) as view:
        yield view
