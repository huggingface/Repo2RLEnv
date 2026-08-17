"""Emit probe, base_commit, language — one line each, in that order.

Exit non-zero on anything unreadable so gate 0's `||` fires. Pure stdlib.
"""

import json, sys

try:
    cfg = json.load(open(sys.argv[1]))
    out = [cfg["probe"], cfg["base_commit"], cfg["language"]]
except Exception:
    sys.exit(1)

# A missing/blank value, or one containing a newline, would desync the three
# `read -r` calls in gate 0 and silently shift base_commit into $R2E_LANG.
if not all(isinstance(v, str) and v.strip() and "\n" not in v for v in out):
    sys.exit(1)

print("\n".join(out))
