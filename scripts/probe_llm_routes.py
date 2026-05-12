"""Smoke-probe every LLM route the v0.8.1 matrix orchestrator will use.

Run before kicking off the matrix to catch auth / route / model-id issues
without burning a full bootstrap cycle. Each route gets a single
"say hello" call; we report success / cost / response snippet per row.

Usage:
    HF_TOKEN=... ANTHROPIC_API_KEY=... OPENAI_API_KEY=... \\
      uv run python scripts/probe_llm_routes.py

Exits 0 if every route returns content, non-zero otherwise.
"""

from __future__ import annotations

import sys
import time

from repo2rlenv.llm import complete
from repo2rlenv.spec.input import LLMSpec

ROUTES: list[tuple[str, LLMSpec]] = [
    (
        "claude-sonnet-4-6",
        LLMSpec(provider="anthropic", model="claude-sonnet-4-6"),
    ),
    (
        "claude-opus-4-7",
        LLMSpec(provider="anthropic", model="claude-opus-4-7"),
    ),
    (
        "gpt-5.5",
        LLMSpec(provider="openai", model="gpt-5.5"),
    ),
    (
        "qwen3-coder-480b via HF/Together",
        LLMSpec(
            provider="huggingface",
            model="Qwen/Qwen3-Coder-480B-A35B-Instruct:together",
        ),
    ),
]


def main() -> int:
    failures = 0
    print(f"{'label':45s}  {'status':8s}  {'cost':>10s}  {'snippet'}")
    print("-" * 110)
    for label, spec in ROUTES:
        t0 = time.monotonic()
        try:
            # GPT-5+ / Opus 4.7+ use reasoning tokens internally before emitting
            # visible content, so we budget generously here — 8 tokens isn't enough.
            resp = complete(
                spec,
                user="Reply with exactly the word PONG. Nothing else.",
                max_tokens=256,
                temperature=0.0,
            )
            dt = time.monotonic() - t0
            snippet = resp.content.strip().replace("\n", " ")[:50]
            status = "ok" if resp.content.strip() else "empty"
            if status != "ok":
                failures += 1
            print(f"{label:45s}  {status:8s}  ${resp.cost_usd:>8.5f}  [{dt:4.1f}s] {snippet}")
        except Exception as exc:
            failures += 1
            print(f"{label:45s}  FAIL      —          {type(exc).__name__}: {str(exc)[:80]}")

    print("-" * 110)
    if failures:
        print(f"{failures}/{len(ROUTES)} routes failed")
        return 1
    print(f"all {len(ROUTES)} routes ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
