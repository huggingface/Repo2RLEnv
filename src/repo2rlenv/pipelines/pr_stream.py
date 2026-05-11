"""Continuous PR mining — `pr_runtime` + state.

Re-running the same command later picks up where the previous run left off:
only PRs merged after the watermark are processed, the new tasks are written
into out_dir alongside the existing ones, and the watermark advances.

This is the SWE-bench-Live recipe (Microsoft, NIPS '25, arXiv:2505.23419):
monthly cron → fresh PRs → contamination-resistant evaluation set. We let
users scope it to any repo, not just a curated few.

Internally we COMPOSE `pr_runtime` rather than re-implement mining +
validation. Per-PR logic is unchanged; the wrapper just manages the
`since=` filter and the watermark file.

----------------------------------------------------------------------------
Acknowledgment
----------------------------------------------------------------------------
Inspired by:

  SWE-bench-Live: A Live Benchmark for Issue Resolving
  (Zhang et al., NIPS '25, arXiv:2505.23419)
  https://github.com/microsoft/SWE-bench-Live   (MIT)

The continuous-mining recipe (cutoff_date + watermark + monthly schedule)
is adapted from their curation pipeline. No code is copied; the scheduler
side is a thin Python orchestration over `gh pr list --merged-at >= …`.

Released under Apache-2.0.
----------------------------------------------------------------------------
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import ClassVar

from repo2rlenv.bootstrap.spec import BootstrapResult
from repo2rlenv.pipelines import _stream_state
from repo2rlenv.pipelines.base import PipelineResult
from repo2rlenv.pipelines.pr_runtime import PRRuntimePipeline
from repo2rlenv.spec.input import GenerationInput, PipelineName
from repo2rlenv.spec.options import PRRuntimeOptions, PRStreamOptions

logger = logging.getLogger(__name__)


def _max_iso(a: str | None, b: str | None) -> str | None:
    """Lex-max of two ISO8601 strings; None is treated as -∞."""
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _iso_from_date(d: date | None) -> str | None:
    return d.isoformat() if d is not None else None


def _date_from_iso(s: str | None) -> date | None:
    if not s:
        return None
    # gh pr list returns "2026-04-12T08:15:22Z"; date.fromisoformat needs
    # YYYY-MM-DD. Take the date portion.
    head = s.split("T", 1)[0]
    try:
        return date.fromisoformat(head)
    except ValueError:
        return None


class PRStreamPipeline:
    """Stateful continuous PR mining. Implements the `Pipeline` Protocol."""

    name: ClassVar[PipelineName] = PipelineName.PR_STREAM
    requires_bootstrap: ClassVar[bool] = True

    def __init__(
        self,
        input: GenerationInput,
        options: PRStreamOptions,
        bootstrap: BootstrapResult | None = None,
    ):
        if bootstrap is None:
            raise RuntimeError(
                "pr_stream requires a BootstrapResult (set requires_bootstrap=True "
                "and let cmd_generate trigger it, or pass one explicitly)"
            )
        self.input = input
        self.options = options
        self.bootstrap = bootstrap
        self._progress_cb = None

    def set_progress_callback(self, cb) -> None:
        self._progress_cb = cb

    # ----- run loop -----------------------------------------------------------

    def run(self, out_dir: Path) -> PipelineResult:
        owner_name = "/".join(self.input.repo.owner_name)
        cache_dir = Path(self.options.state_dir).expanduser().resolve()

        # 1. Load watermark; compute effective `since`
        state = _stream_state.load(owner_name, cache_dir)
        watermark_date = _date_from_iso(state.last_merged_at)
        effective_since = self._choose_since(
            watermark=watermark_date,
            cutoff=self.options.cutoff_date,
            user_since=self.options.since,
        )
        logger.info(
            "pr_stream %s — watermark=%s cutoff=%s effective_since=%s",
            owner_name,
            state.last_merged_at,
            self.options.cutoff_date,
            effective_since,
        )

        # 2. Build an inner PRRuntimeOptions with the computed `since`
        inner_opts = self._build_inner_options(effective_since)

        # 3. Delegate to PRRuntimePipeline (mining + validation + emission)
        inner = PRRuntimePipeline(self.input, inner_opts, bootstrap=self.bootstrap)
        if self._progress_cb is not None:
            inner.set_progress_callback(self._progress_cb)

        # 4. The inner pipeline writes tasks straight into out_dir — emit
        #    paths are deterministic (owner__repo-prNumber), so a re-run
        #    that hits a previously-emitted PR just overwrites the same dir.
        #    We don't filter out already-emitted PRs at the pipeline layer;
        #    the watermark on `since` does it.
        result = inner.run(out_dir)

        # 5. Advance watermark from the just-emitted tasks
        new_state = self._advance_state_from_results(state, out_dir, result)
        _stream_state.save(new_state, cache_dir)
        logger.info(
            "pr_stream %s — advanced watermark %s → %s; emitted=%d",
            owner_name,
            state.last_merged_at,
            new_state.last_merged_at,
            result.emitted,
        )
        return result

    # ----- helpers ------------------------------------------------------------

    def _choose_since(
        self,
        *,
        watermark: date | None,
        cutoff: date | None,
        user_since: date | None,
    ) -> date | None:
        """Effective lower bound for `merged_at`: max of all three (latest wins)."""
        candidates = [d for d in (watermark, cutoff, user_since) if d is not None]
        if not candidates:
            return None
        return max(candidates)

    def _build_inner_options(self, since: date | None) -> PRRuntimeOptions:
        """Project PRStreamOptions onto PRRuntimeOptions for the inner pipeline.

        PRStreamOptions inherits PRRuntimeOptions, so most fields just
        carry over. We override `since` with the watermark-derived value.
        """
        payload = self.options.model_dump()
        # Drop stream-only fields the inner pipeline doesn't know
        for k in ("cutoff_date", "state_dir"):
            payload.pop(k, None)
        payload["since"] = since
        return PRRuntimeOptions.model_validate(payload)

    def _advance_state_from_results(
        self,
        prev_state: _stream_state.StreamState,
        out_dir: Path,
        result: PipelineResult,
    ) -> _stream_state.StreamState:
        """Read merged_at from each emitted task.toml, advance the watermark."""
        merged_ats: list[str] = []
        emitted_numbers: list[int] = list(prev_state.emitted_pr_numbers)
        for task_dir in out_dir.iterdir() if out_dir.exists() else []:
            if not task_dir.is_dir():
                continue
            toml_path = task_dir / "task.toml"
            if not toml_path.is_file():
                continue
            try:
                import tomllib

                data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
            except (tomllib.TOMLDecodeError, OSError):
                continue
            r2e = data.get("metadata", {}).get("repo2env", {})
            inner = r2e.get("pr_runtime", {}) or r2e.get("pr_stream", {})
            merged_at = inner.get("pr_merged_at")
            if isinstance(merged_at, str):
                merged_ats.append(merged_at)
            # Track PR number for dedup
            url = inner.get("pr_url", "")
            if "/pull/" in url:
                try:
                    n = int(url.rsplit("/", 1)[-1])
                    if n not in emitted_numbers:
                        emitted_numbers.append(n)
                except ValueError:
                    pass
        advanced = _stream_state.advance_watermark(prev_state, merged_ats)
        return _stream_state.StreamState(
            repo=advanced.repo,
            last_merged_at=_max_iso(advanced.last_merged_at, prev_state.last_merged_at),
            emitted_pr_numbers=emitted_numbers,
        )
