#!/usr/bin/env bash
set -euo pipefail
python /work/recipes/runtime/summarize_remote.py > /evidence/summary-stdout.json
python /work/recipes/runtime/collect_artifacts.py "${REPRO_SNAPSHOT:-/evidence/snapshot-final.tar.gz}"
