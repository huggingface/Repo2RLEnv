#!/usr/bin/env bash
set -euo pipefail
export REPRO_PROJECT=endless
export REPRO_NATIVE=/work/endless/native-batch-02
export REPRO_HARBOR=/work/endless/harbor-batch-02
export REPRO_RUN=batch-02-probe-01
export REPRO_MODEL=gpt-4o
bash /work/recipes/runtime/probe_terminal_tasks.sh
