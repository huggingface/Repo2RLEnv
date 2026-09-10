#!/usr/bin/env bash
set -euo pipefail
# Set these explicitly in the calling remote shell to keep experiments separate.
: "${REPRO_PROJECT:?endless or tmax}" "${REPRO_NATIVE:?}" "${REPRO_HARBOR:?}" "${REPRO_RUN:?}" "${REPRO_MODEL:?}"
export PATH="/work/$REPRO_PROJECT/venv/bin:$PATH"
export PYTHONPATH="/work/recipes/runtime:/work/$REPRO_PROJECT/upstream"
export REPRO_API_RESPONSES="/evidence/$REPRO_PROJECT/$REPRO_RUN-api.jsonl"
export PYTHONUNBUFFERED=1
cd "/work/$REPRO_PROJECT/upstream"
for task in "$REPRO_NATIVE"/task_*; do
    [[ -d "$task" ]] || continue
    harbor_task="$REPRO_HARBOR/$(basename "$task")"
    [[ -f "$harbor_task/task.toml" ]] || continue
    python /work/recipes/runtime/native_terminal_probe.py "$REPRO_PROJECT" "$task" "/evidence/$REPRO_PROJECT/$REPRO_RUN/$(basename "$task")" --model "$REPRO_MODEL" --harbor-task "$harbor_task" --max-actions 20
    python /work/recipes/runtime/harbor_artifacts.py audit "$harbor_task" "/evidence/$REPRO_PROJECT/$REPRO_RUN-harbor/$(basename "$task")"
done
