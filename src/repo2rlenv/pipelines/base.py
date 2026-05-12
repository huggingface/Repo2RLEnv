"""Pipeline contract — every synthesis pipeline implements this Protocol.

A pipeline:
  1. Takes a `GenerationInput` (the standard input shape) plus its own Options
     model (the validated kwargs).
  2. Emits Harbor-compatible task directories at `out_dir`.
  3. Returns a `PipelineResult` with candidate / emitted / skipped counters
     so the CLI can report yield + QA pass rates uniformly.

Adding a new pipeline = subclass-by-protocol (just match the shape) + register
in `PIPELINES` and `OPTIONS_REGISTRY`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel

from repo2rlenv.bootstrap.spec import LanguageHint
from repo2rlenv.spec.input import GenerationInput, PipelineName

logger = logging.getLogger(__name__)


class LanguageMismatchError(RuntimeError):
    """Pipeline can't run against the detected repo language.

    Raised by `check_language_compatibility()` when a Python-only pipeline
    is pointed at a Go / Rust / Node repo without `--force-language`.
    """


@dataclass(slots=True)
class PipelineResult:
    """Uniform result shape across every pipeline.

    Attributes:
        candidates: Total candidates discovered before filtering.
        emitted: Tasks actually written to `out_dir`.
        skipped: Sum of skip-reason counts.
        out_dir: Where tasks landed.
        skip_reasons: Per-reason counts, e.g. {"draft": 3, "too_many_files": 2}.
    """

    candidates: int
    emitted: int
    skipped: int
    out_dir: Path
    skip_reasons: dict[str, int]


@runtime_checkable
class Pipeline(Protocol):
    """The contract every synthesis pipeline implements.

    Implementations are duck-typed (Protocol, not ABC) so a class doesn't have
    to inherit from anything — just expose:

      - `name: ClassVar[PipelineName]` — the registered identifier
      - `requires_bootstrap: ClassVar[bool]` — True if the pipeline needs a
        working Docker image from the bootstrap phase before it can run
      - `__init__(input, options, bootstrap=None) -> None`
      - `run(out_dir: Path) -> PipelineResult`

    The `Options` arg is whatever Pydantic model is registered for `name` in
    `OPTIONS_REGISTRY`. The dispatcher in `cli.cmd_generate` validates and
    instantiates both before calling `run()`.

    `bootstrap` carries the BootstrapResult (image_digest + test_cmds + ...)
    when `requires_bootstrap=True`. cmd_generate triggers `ensure_bootstrap()`
    automatically; lite pipelines that set `requires_bootstrap=False` get
    `bootstrap=None`.
    """

    name: ClassVar[PipelineName]
    requires_bootstrap: ClassVar[bool] = False
    # Languages this pipeline can handle. `None` means any language is OK.
    # Set to a frozenset of LanguageHint values to restrict (e.g. Python-only
    # pipelines that parse AST or emit pytest verifiers).
    supported_languages: ClassVar[frozenset[LanguageHint] | None] = None

    def __init__(
        self,
        input: GenerationInput,
        options: BaseModel,
        bootstrap: Any = None,
    ) -> None: ...

    def run(self, out_dir: Path) -> PipelineResult: ...


def check_language_compatibility(
    pipeline_cls: type,
    detected: LanguageHint,
    *,
    force: bool = False,
) -> None:
    """Verify the pipeline can run against the detected repo language.

    Behavior:
      - `pipeline_cls.supported_languages is None` → any language is fine; returns.
      - detected ∈ supported_languages → returns.
      - mismatch + `force=True` → logs a warning; returns.
      - mismatch + `force=False` → raises `LanguageMismatchError`.
    """
    supported = getattr(pipeline_cls, "supported_languages", None)
    if supported is None:
        return
    if detected in supported:
        return

    pipeline_name = getattr(pipeline_cls, "name", "<unknown>")
    supported_names = ", ".join(sorted(s.value for s in supported))
    detected_name = detected.value if isinstance(detected, LanguageHint) else str(detected)

    if force:
        logger.warning(
            "pipeline %s requires %s; detected %s. Proceeding because --force-language is set.",
            pipeline_name,
            supported_names,
            detected_name,
        )
        return

    raise LanguageMismatchError(
        f"Pipeline {pipeline_name!r} requires {supported_names}; "
        f"this repo is detected as {detected_name!r}.\n"
        f"\n"
        f"This pipeline can't produce valid tasks on {detected_name} code. Options:\n"
        f"  - Pick a language-agnostic pipeline "
        f"(pr_runtime / commit_runtime / cve_patches / pr_diff / pr_stream)\n"
        f"  - Re-run with --force-language to skip this check "
        f"(the pipeline will likely emit 0 tasks)"
    )
