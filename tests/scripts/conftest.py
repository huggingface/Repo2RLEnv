"""Make `scripts/v083/` importable by pytest as `v083_sweep`, etc.

These scripts live outside the published package (`src/repo2rlenv/`) on
purpose — they're launch-sweep tooling, not part of the library. Tests
still need to import them, so we shim the path here.
"""

from __future__ import annotations

import sys
from pathlib import Path

_V083 = Path(__file__).resolve().parents[2] / "scripts" / "v083"
if str(_V083) not in sys.path:
    sys.path.insert(0, str(_V083))
