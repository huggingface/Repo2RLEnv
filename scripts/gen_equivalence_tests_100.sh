#!/bin/bash
# scripts/gen_equivalence_tests_100.sh
#
# Generate 100 equivalence_tests environments across a broad set of
# utility-heavy Python repos, then (optionally) push to HF Hub.
#
# Usage:
#   ./scripts/gen_equivalence_tests_100.sh              # generate only, WITH Rich UI
#   PUSH_HF=1 ./scripts/gen_equivalence_tests_100.sh    # generate + push to HF Hub
#   LOG_FILE=1 ./scripts/gen_equivalence_tests_100.sh   # capture output to gen.log (no UI)
#   HF_DATASET=YourOrg/name ./scripts/gen_equivalence_tests_100.sh   # override target
#   TARGET=200 ./scripts/gen_equivalence_tests_100.sh   # generate more envs
#
# UI vs log tradeoff:
#   * Default: Rich UI streams to your terminal (Live panel, colored logs).
#     No file capture — if the run dies, terminal scrollback is your only
#     record.
#   * `LOG_FILE=1`: tee output to workspace/datasets/eqv-100/gen.log.
#     Rich auto-degrades to plain text when stdout is a pipe, so you lose
#     the panel but gain a persistent, greppable log.
#
# The script:
#   - Sources ./.env for API keys (ANTHROPIC_API_KEY, HF_TOKEN, ...)
#   - Runs repo2rlenv generate --pipeline equivalence_tests per repo, in
#     order-of-candidate-density (best repos first)
#   - STOPS AS SOON AS the total emitted count crosses $TARGET (default 100)
#   - Bootstraps missing images on demand (first pass ≈ 5-15 min per repo);
#     subsequent runs cache-hit
#   - Skips repo subdirs already populated in this run (idempotent re-runs)
#   - Logs to workspace/datasets/eqv-100/gen.log
#
# Diversity: per-repo caps balance yield with dataset variety, so sympy
# (with 143 candidates) doesn't dominate the emitted set. Sum of caps is
# ~250 candidates, giving ~5-10x oversampling headroom at typical
# Stage-B pass rates (~40-60%).
#
# Wall clock: highly variable. First-time bootstraps of large repos
# (django, setuptools, sympy) can take 10-20 min each. Expect
# 45-120 min end-to-end on a laptop with default concurrency.
#
# LLM spend: ~$10-25 depending on retry churn.

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
LOG_FILE="${LOG_FILE:-0}"

mkdir -p "$OUT_DIR"
touch "$LOG"

# Helper: if LOG_FILE=1, pipe through tee; else pass through directly so
# Rich's Live UI + colored RichHandler output reach the terminal.
if [ "$LOG_FILE" = "1" ]; then
    echo "===== equivalence_tests $TARGET-env generation (LOG_FILE mode) ====="  | tee -a "$LOG"
    echo "target=$TARGET  out=$OUT_DIR  push_hf=$PUSH_HF  hf_dataset=$HF_DATASET" | tee -a "$LOG"
    date | tee -a "$LOG"
else
    echo "===== equivalence_tests $TARGET-env generation (UI mode) ====="
    echo "target=$TARGET  out=$OUT_DIR  push_hf=$PUSH_HF  hf_dataset=$HF_DATASET"
    date
fi

# Convenience: swap `tee -a "$LOG"` in when LOG_FILE=1, else pass-through.
_out() {
    if [ "$LOG_FILE" = "1" ]; then
        tee -a "$LOG"
    else
        cat
    fi
}

# Repos ordered by pure-candidate density (`walk_repo` count), with
# per-repo caps that balance diversity. Each entry: `owner/name:cap`.
# Sum of caps: ~250 → ~5x headroom at 40-60% Stage-B pass.
#
# Numbers in comments are the raw candidate counts observed during
# the v0.8.7 audit probe (`_function_extractor.walk_repo` output).
declare -a REPOS=(
    # Tier 1 — math & core utilities (huge yield)
    "sympy/sympy:30"                  # 143 candidates
    "mpmath/mpmath:20"                # 42
    "hypothesisworks/hypothesis:15"   # 25
    "more-itertools/more-itertools:12" # 15
    "Suor/funcy:10"                   # 10

    # Tier 2 — packaging / dev tools (bootstrap-cheap, well-tested)
    "django/django:20"                # 56
    "pypa/setuptools:15"              # 35
    "pypa/pip:15"                     # 29
    "pytest-dev/pytest:12"            # 15
    "sqlalchemy/sqlalchemy:10"        # 14
    "pygments/pygments:10"            # 14

    # Tier 3 — HuggingFace ecosystem
    "huggingface/accelerate:12"       # 17
    "huggingface/evaluate:8"          # 9
    "huggingface/huggingface_hub:6"   # 6
    "huggingface/tokenizers:3"        # 2
    "huggingface/safetensors:3"       # 1

    # Tier 4 — smaller but useful utility libs
    "psf/black:7"                     # 7
    "tiangolo/typer:5"                # 7
    "google/yapf:5"                   # 5
    "python-babel/babel:5"            # 5
    "huggingface/optimum:5"           # 5
    "python-jsonschema/jsonschema:4"  # 4
    "pallets/werkzeug:3"              # 3

    # Tier 5 — original v0.8.7 audit set (kept for regression continuity)
    "psf/requests:4"                  # 4
    "python-attrs/attrs:2"            # 2
    "pallets/jinja:2"                 # 2
    "pypa/packaging:2"                # 2
    "encode/httpx:2"                  # 2
    "pallets/click:2"                 # 2

    # Tier 6 — fallback single-candidate repos (only reached if we're
    # struggling; each contributes 1 emit at most)
    "simplejson/simplejson:1"         # 1
    "gruns/furl:1"                    # 1
    "lark-parser/lark:2"              # 2
    "prompt-toolkit/python-prompt-toolkit:2" # 2
    "python-poetry/poetry:1"          # 1
    "MongoEngine/mongoengine:3"       # 3
    "pypa/pipx:2"                     # 2
    "ronf/asyncssh:2"                 # 2
    "pdm-project/pdm:2"               # 2
    "python-openxml/python-docx:2"    # 2
    "encode/starlette:1"              # 0-1
    "dateutil/dateutil:1"             # 1
)

count_emits() {
    # `|| true` on grep because grep exits 1 when no lines match, which
    # combined with `set -e -o pipefail` at the top would kill the script
    # silently on the first iteration (when the out dir is empty).
    find "$OUT_DIR" -maxdepth 2 -mindepth 2 -type d 2>/dev/null \
        | { grep -v '.debug_skips' || true; } | wc -l | tr -d ' '
}

for entry in "${REPOS[@]}"; do
    repo=${entry%:*}
    limit=${entry#*:}
    slug=${repo##*/}

    current=$(count_emits)
    if [ "$current" -ge "$TARGET" ]; then
        echo "===== HIT $TARGET — stopping =====" | _out
        break
    fi

    # Idempotent re-run — skip repos already populated
    already=$(
        find "$OUT_DIR/$slug" -maxdepth 1 -mindepth 1 -type d 2>/dev/null \
            | { grep -v '.debug_skips' || true; } | wc -l | tr -d ' '
    )
    if [ -d "$OUT_DIR/$slug" ] && [ "$already" -gt 0 ]; then
        echo "----- $repo -> $slug: already has $already envs, skipping" | _out
        continue
    fi

    echo "===== $repo -> $slug (limit=$limit, so far=$current/$TARGET) =====" | _out
    date | _out

    if [ "$LOG_FILE" = "1" ]; then
        # Pipe through tee for a persistent log; Rich degrades to plain text.
        uv run repo2rlenv generate \
            --repo "$repo" \
            --pipeline equivalence_tests \
            --pipeline-opt "limit=$limit" \
            --pipeline-opt "seed=1" \
            --llm anthropic/claude-sonnet-4-6 \
            --out "$OUT_DIR/$slug" 2>&1 | tee -a "$LOG" | tail -12 || {
                echo "!! $repo generation errored — continuing to next repo" | tee -a "$LOG"
                continue
            }
    else
        # No pipe: Rich sees a TTY and shows the Live UI + colored logs.
        uv run repo2rlenv generate \
            --repo "$repo" \
            --pipeline equivalence_tests \
            --pipeline-opt "limit=$limit" \
            --pipeline-opt "seed=1" \
            --llm anthropic/claude-sonnet-4-6 \
            --out "$OUT_DIR/$slug" || {
                echo "!! $repo generation errored — continuing to next repo"
                continue
            }
    fi
done

final=$(count_emits)
echo "===== GENERATION COMPLETE: $final / $TARGET envs =====" | _out
date | _out

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

echo "===== Pushing to $HF_DATASET =====" | _out
uv run repo2rlenv push \
    "$FLAT_DIR" \
    "$HF_DATASET" \
    --inline-dockerfile \
    --message "Repo2RLEnv: equivalence_tests v0.7.1 — $flat_count function-level equivalence-test envs across utility-heavy Python repos"

echo "===== DONE =====" | _out
date | _out
echo "Dataset: https://huggingface.co/datasets/$HF_DATASET"
echo ""
echo "Next: ./scripts/add_to_collection.sh $HF_DATASET \"equivalence_tests v0.7.1 — <N> envs across ~30 Python utility libs\""
