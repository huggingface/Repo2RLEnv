#!/usr/bin/env bash
set -euo pipefail
export REPRO_PROJECT=endless
export REPRO_NATIVE=/work/endless/native-batch-01
export REPRO_HARBOR=/work/endless/harbor
export REPRO_RUN=probe-04
export REPRO_MODEL=gpt-4o
git -C /work/endless/upstream apply /work/recipes/endless-terminals/patches/004-modal-userns.patch
bash /work/recipes/runtime/probe_terminal_tasks.sh
