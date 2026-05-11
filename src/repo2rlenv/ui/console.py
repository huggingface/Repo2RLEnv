"""The shared Repo2RLEnv console + logger integration.

`console` is a module-level singleton everyone imports. Methods cover the
common stamped-line patterns (success/info/warn/error), section headers,
and key-value panels.

Logging is routed through `rich.logging.RichHandler` so plain log lines from
pipelines and runners get colored consistently with the rest of the UI.
When a Live is active, noisy library loggers (litellm/httpx) are temporarily
silenced so they don't tear the live display.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from typing import Any, ContextManager, Iterator

from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel

from repo2rlenv.ui.primitives import kv_panel
from repo2rlenv.ui.theme import GLYPH, STYLE


def _should_use_rich() -> bool:
    """Use Rich UI only on a real terminal. Respect NO_COLOR / CI / dumb."""
    if os.environ.get("NO_COLOR") or os.environ.get("CI"):
        return False
    if not sys.stdout.isatty():
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return True


class R2EConsole:
    """Single-instance wrapper around rich.console.Console.

    Adds stamped-line helpers (success/info/warn/error) and tracks whether a
    Live is currently active so noisy log streams can be suppressed.
    """

    def __init__(self, *, force_terminal: bool | None = None):
        self.console = Console(force_terminal=force_terminal)
        self.live_active = False

    # ----- stamped lines ------------------------------------------------------

    def success(self, msg: str) -> None:
        self.console.print(f"[{STYLE.SUCCESS}]{GLYPH.SUCCESS}[/] {msg}")

    def info(self, msg: str) -> None:
        self.console.print(f"[{STYLE.INFO}]{GLYPH.INFO}[/] {msg}")

    def warn(self, msg: str) -> None:
        self.console.print(f"[{STYLE.WARN}]{GLYPH.WARN}[/] {msg}")

    def error(self, msg: str) -> None:
        self.console.print(f"[{STYLE.ERROR}]{GLYPH.ERROR}[/] {msg}")

    def dim(self, msg: str) -> None:
        self.console.print(f"[{STYLE.DIM}]{msg}[/]")

    # ----- structured output --------------------------------------------------

    @contextmanager
    def section(self, title: str) -> Iterator[None]:
        """Context manager that prints a rule before/after a block of output."""
        self.console.rule(f"[{STYLE.HEADER}]{title}[/]", style=STYLE.HEADER)
        try:
            yield
        finally:
            self.console.rule(style=STYLE.DIM)

    def kv(self, pairs: dict[str, Any], *, title: str | None = None) -> None:
        """Render a key/value table inside a panel."""
        self.console.print(kv_panel(pairs, title=title))

    def panel(self, body: Any, *, title: str | None = None, style: str = STYLE.PANEL_INFO) -> None:
        self.console.print(Panel(body, title=title, border_style=style, expand=False))

    # ----- escape hatch -------------------------------------------------------

    def print(self, *args: Any, **kwargs: Any) -> None:
        self.console.print(*args, **kwargs)


def _make_default_console() -> R2EConsole:
    return R2EConsole()


# Module-level singleton. Import this everywhere.
console: R2EConsole = _make_default_console()


# ---------------------------------------------------------------------------
# Logging integration
# ---------------------------------------------------------------------------

_LOGGING_INSTALLED = False


def install_logging(*, level: int = logging.INFO, propagate_noisy: bool = False) -> None:
    """Route logging through Rich so log lines blend with console output.

    Idempotent. Call once at CLI entry. `propagate_noisy=False` mutes
    litellm/httpx/anthropic chatter at the root level so it doesn't dominate
    output; the noisy libraries still emit at WARNING+.
    """
    global _LOGGING_INSTALLED
    if _LOGGING_INSTALLED:
        return
    _LOGGING_INSTALLED = True

    handler = RichHandler(
        console=console.console,
        rich_tracebacks=True,
        markup=True,
        show_path=False,
        show_time=False,
        show_level=True,
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    if not propagate_noisy:
        for noisy in ("litellm", "LiteLLM", "httpx", "httpcore",
                       "anthropic", "openai"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def should_use_rich() -> bool:  # re-export for views
    return _should_use_rich()
