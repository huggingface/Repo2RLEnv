#!/usr/bin/env bash
# Thin shim around runner.py — the real implementation lives there.
# See `python3 examples/crabbox/runner.py --help`.
exec python3 "$(dirname "$0")/runner.py" "$@"
