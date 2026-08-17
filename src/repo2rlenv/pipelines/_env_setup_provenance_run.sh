#!/bin/bash
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LANG_ID="$1"; R2E_PROBE="$2"
set +u; . "$SCRIPT_DIR/env_prelude.sh"; set -u
case "$LANG_ID" in
  python) exec python3 "$SCRIPT_DIR/provenance.py" "$SCRIPT_DIR/provenance.json" "$R2E_PROBE" ;;
  node)   exec node    "$SCRIPT_DIR/provenance.js" "$SCRIPT_DIR/provenance.json" ;;
  *)      exit 0 ;;
esac
