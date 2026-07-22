#!/bin/bash
# scripts/gen_equivalence_tests_100.sh
#
# Generate 100 equivalence_tests environments across a broad set of
# utility-heavy Python repos, then (optionally) push to HF Hub.
#
# Usage:
#   ./scripts/gen_equivalence_tests_100.sh              # generate only
#   PUSH_HF=1 ./scripts/gen_equivalence_tests_100.sh    # generate + push to HF Hub
#   HF_DATASET=YourOrg/name ./scripts/gen_equivalence_tests_100.sh   # override target
#
# The script:
#   - Sources ./.env for API keys (ANTHROPIC_API_KEY, HF_TOKEN, ...)
#   - Runs repo2rlenv generate --pipeline equivalence_tests per repo, in order
#   - STOPS AS SOON AS the total emitted count crosses 100
#   - Bootstraps missing images on demand (first pass ≈ 5-15 min per repo);
#     subsequent runs cache-hit
#   - Skips repos already generated in this run (idempotent)
#   - Logs to workspace/datasets/eqv-100/gen.log
#
# Wall clock: highly variable. First-time bootstraps dominate. Expect
# 30-90 min end-to-end on a laptop with concurrency=1 (default).
#
# LLM spend: ~$5-20 depending on retry churn on hard candidates.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Load API keys
if [ -f .env ]; then
    set -a; source ./.env; set +a
else
    echo "warn: no .env at repo root — assuming API keys are already in the environment" >&2
fi

OUT_DIR="workspace/datasets/eqv-100"
FLAT_DIR="workspace/datasets/eqv-100-flat"
LOG="$OUT_DIR/gen.log"
HF_DATASET="${HF_DATASET:-AdithyaSK/repo2rlenv-equivalence-tests}"
PUSH_HF="${PUSH_HF:-0}"
TARGET="${TARGET:-100}"

mkdir -p "$OUT_DIR"
: > "$LOG"

echo "===== equivalence_tests 100-env generation =====" | tee -a "$LOG"
echo "target=$TARGET  out=$OUT_DIR  push_hf=$PUSH_HF  hf_dataset=$HF_DATASET" | tee -a "$LOG"
date | tee -a "$LOG"

# Repos ordered roughly by pure-candidate density (higher first). Each entry
# is `owner/name:per-repo-limit`. Per-repo limits are ceilings; the pipeline
# will emit fewer if it exhausts candidates. The outer loop stops as soon as
# the total emitted count crosses $TARGET, so extra repos in this list are
# defensive slack.
declare -a REPOS=(
    "django/django:35"
    "pypa/setuptools:25"
    "pypa/pip:20"
    "pytest-dev/pytest:15"
    "sqlalchemy/sqlalchemy:15"
    "huggingface/huggingface_hub:10"
    "psf/black:10"
    "tiangolo/typer:10"
    "python-jsonschema/jsonschema:8"
    "pallets/werkzeug:8"
    "psf/requests:5"
    "python-attrs/attrs:5"
    "pallets/jinja:5"
    "pypa/packaging:5"
    "simplejson/simplejson:5"
    "encode/httpx:3"
    "pallets/click:3"
    "encode/starlette:3"
    "huggingface/safetensors:3"
    "huggingface/tokenizers:3"
    "aio-libs/yarl:3"
    "python-hyper/h11:3"
    "python-openxml/python-docx:3"
    "pdm-project/pdm:3"
    "tox-dev/tox:3"
    "pypa/pipx:3"
    "ronf/asyncssh:3"
    "tiangolo/fastapi-cli:3"
    "psf/black:3"
    "dateutil/dateutil:3"
)

count_emits() {
    find "$OUT_DIR" -maxdepth 2 -mindepth 2 -type d 2>/dev/null \
        | grep -v '.debug_skips' | wc -l | tr -d ' '
}

for entry in "${REPOS[@]}"; do
    repo=${entry%:*}
    limit=${entry#*:}
    slug=${repo##*/}

    current=$(count_emits)
    if [ "$current" -ge "$TARGET" ]; then
        echo "===== HIT $TARGET — stopping =====" | tee -a "$LOG"
        break
    fi

    # Skip repos already generated in this run (idempotent re-run)
    if [ -d "$OUT_DIR/$slug" ] && [ "$(find "$OUT_DIR/$slug" -maxdepth 1 -mindepth 1 -type d | grep -v '.debug_skips' | wc -l | tr -d ' ')" -gt 0 ]; then
        already=$(find "$OUT_DIR/$slug" -maxdepth 1 -mindepth 1 -type d | grep -v '.debug_skips' | wc -l | tr -d ' ')
        echo "----- $repo -> $slug: already has $already envs, skipping" | tee -a "$LOG"
        continue
    fi

    echo "===== $repo -> $slug (limit=$limit, so far=$current/$TARGET) =====" | tee -a "$LOG"
    date | tee -a "$LOG"

    uv run repo2rlenv generate \
        --repo "$repo" \
        --pipeline equivalence_tests \
        --pipeline-opt "limit=$limit" \
        --pipeline-opt "seed=1" \
        --llm anthropic/claude-sonnet-4-6 \
        --out "$OUT_DIR/$slug" 2>&1 | tee -a "$LOG" | tail -10 || {
            echo "!! $repo generation errored — continuing to next repo" | tee -a "$LOG"
            continue
        }
done

final=$(count_emits)
echo "===== GENERATION COMPLETE: $final / $TARGET envs =====" | tee -a "$LOG"
date | tee -a "$LOG"

if [ "$final" -lt "$TARGET" ]; then
    echo "warn: only $final / $TARGET envs generated. Consider adding more repos to the REPOS array." >&2
fi

# --------------------------------------------------------------------------
# Optional HF Hub push
# --------------------------------------------------------------------------

if [ "$PUSH_HF" != "1" ]; then
    echo "PUSH_HF is not 1 — stopping here. Set PUSH_HF=1 to publish." | tee -a "$LOG"
    exit 0
fi

echo "===== Flattening to $FLAT_DIR =====" | tee -a "$LOG"
rm -rf "$FLAT_DIR"
mkdir -p "$FLAT_DIR"
for repo_dir in "$OUT_DIR"/*/; do
    [ -d "$repo_dir" ] || continue
    slug=$(basename "$repo_dir")
    [ "$slug" = "jobs" ] && continue
    for task_dir in "$repo_dir"/*__*; do
        [ -d "$task_dir" ] || continue
        cp -r "$task_dir" "$FLAT_DIR/"
    done
done
flat_count=$(find "$FLAT_DIR" -maxdepth 1 -mindepth 1 -type d | wc -l | tr -d ' ')
echo "flat tasks: $flat_count" | tee -a "$LOG"

echo "===== Pushing to $HF_DATASET =====" | tee -a "$LOG"
uv run repo2rlenv push \
    "$FLAT_DIR" \
    "$HF_DATASET" \
    --inline-dockerfile \
    --message "Repo2RLEnv: equivalence_tests v0.7.1 — $flat_count function-level equivalence-test envs across utility-heavy Python repos" \
    2>&1 | tee -a "$LOG"

echo "===== DONE =====" | tee -a "$LOG"
date | tee -a "$LOG"
echo "Dataset: https://huggingface.co/datasets/$HF_DATASET"
