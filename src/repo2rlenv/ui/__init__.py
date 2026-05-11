"""Unified Rich-based UI + logging for every Repo2RLEnv CLI command.

One module, one Console, one set of glyphs and colors. Use the primitives
from anywhere; build a `live_view()` when you need a redrawing display.

```python
from repo2rlenv.ui import console

console.success("Pushed dataset to AdithyaSK/trl-r2e-v0-1")
console.info("Starting bootstrap...")
console.warn("Smoke test exited 5 (no tests collected — env is fine)")
console.error("Bootstrap failed: no internet access")

console.kv({"image_digest": "ghcr.io/...", "iterations": 6, "cost_usd": 0.12},
           title="Bootstrap result")

with console.section("Generation"):
    for task in tasks:
        console.success(f"emitted {task.name}")
```

For long-running tasks with a live display:

```python
from repo2rlenv.ui import BootstrapView, GenerationView

with BootstrapView(...) as view:
    ensure_bootstrap(..., on_turn=view.on_turn,
                          on_phase=view.on_phase, ...)
    view.set_outcome(success=True, image_digest=..., ...)
```

Output is automatically silenced from noisy libraries (litellm, httpx) while
a Live is active so the display doesn't tear.
"""

from repo2rlenv.ui.console import R2EConsole, console, install_logging
from repo2rlenv.ui.live import live_view, quiet_libraries
from repo2rlenv.ui.primitives import (
    error_panel,
    header_panel,
    kv_panel,
    success_panel,
)
from repo2rlenv.ui.theme import GLYPH, STYLE
from repo2rlenv.ui.views.bootstrap import BootstrapView
from repo2rlenv.ui.views.generation import GenerationView

__all__ = [
    "R2EConsole",
    "console",
    "install_logging",
    "live_view",
    "quiet_libraries",
    "STYLE",
    "GLYPH",
    "success_panel",
    "error_panel",
    "kv_panel",
    "header_panel",
    "BootstrapView",
    "GenerationView",
]
