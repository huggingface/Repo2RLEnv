"""One error record for CLI adapters; verbose diagnostics stay on stderr."""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

from repo2rlenv.ui.console import console


def report_error(exc: Exception, *, json_output: bool, verbose: bool = False) -> int:
    if json_output:
        console.json({"error": type(exc).__name__, "message": str(exc)})
    else:
        Console(stderr=True).print(Text(f"{type(exc).__name__}: {exc}"))
    if verbose:
        Console(stderr=True).print_exception(show_locals=False)
    return 2
