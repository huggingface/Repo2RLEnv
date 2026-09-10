#!/usr/bin/env bash
set -euo pipefail
export REPRO_PROJECT=tmax
export REPRO_NATIVE=/work/tmax/native-smoke-01
export REPRO_HARBOR=/work/tmax/harbor-v2
export REPRO_RUN=probe-01
export REPRO_MODEL=anthropic/claude-sonnet-4-6
bash /work/recipes/runtime/probe_terminal_tasks.sh
