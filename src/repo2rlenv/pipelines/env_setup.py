"""Repo2Run/SetupBench-style: hand an agent a bare repo and grade it on making
the repo's own test suite install and run green.

Bootstrap still runs — it's the cheapest way to get a working recipe out of
an agent — but its IMAGE is thrown away. What survives is the transcript (raw
material for the gold recipe), `test_cmds` (what "the suite" means here), and
`language`. The emitted task's `FROM` is the bare base image bootstrap started
from, so the gold recipe distilled from the transcript replays against
identical ground: same base image, same starting commit, nothing pre-baked.

Flow, end to end: bootstrap runs (image discarded) -> the recipe is distilled
from its transcript and proven in a container built from the EXACT Dockerfile
this module emits -> the green run's parsed test list becomes the FAIL_TO_PASS
set -> the emitted `tests/test.sh` is dry-run against that same known-good
container (F') before the task ships. There is no other route to the F2P set
and no other proof that the emitted gates can pass at all.

----------------------------------------------------------------------------
Acknowledgment
----------------------------------------------------------------------------
The "agent-bootstraps-an-environment, then a gold recipe is distilled and
verified from scratch, graded against the repo's OWN test suite from a bare
starting point" shape this pipeline implements is informed by:

  Repo2Run (ByteDance, arXiv:2502.13681)
  https://github.com/bytedance/Repo2Run    (Apache-2.0)

  SetupBench (Microsoft, arXiv:2507.09063)
  https://github.com/microsoft/SetupBench    (MIT)

  EnvBench (JetBrains Research, ICLR '25 DL4Code, arXiv:2503.14443)
  https://github.com/JetBrains-Research/EnvBench    (MIT)

  PEP 610 — Recording the Direct URL Origin of Installed Distributions
  https://peps.python.org/pep-0610/

This module is an INDEPENDENT IMPLEMENTATION — no code is copied from any of
the three prior-art repos. It reuses only the general shape (agent bootstraps
a working environment; a clean recipe is distilled and independently
re-verified from a bare state; the provenance-probe design borrows PEP 610's
non-forgeable signal) and reimplements it from scratch against this repo's
own LLM/Docker primitives plus Python stdlib. None of the upstream licenses
apply to this file; Repo2RLEnv is Apache-2.0.
----------------------------------------------------------------------------
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from pydantic import BaseModel

from repo2rlenv.bootstrap.spec import LanguageHint
from repo2rlenv.sources import Capability, capabilities_for
from repo2rlenv.spec.input import GenerationInput, PipelineName
from repo2rlenv.spec.options import EnvSetupOptions

logger = logging.getLogger(__name__)


class EnvSetupPipeline:
    """Repo2Run/SetupBench-style: the agent makes a bare repo's suite run green.

    Bootstrap still runs, but its IMAGE is thrown away. What we keep is the
    transcript (raw material for the gold recipe), test_cmds (what "the suite"
    means here), and language. The emitted task's FROM is the bare base image
    bootstrap started from, so the gold recipe replays against identical
    ground.
    """

    name: ClassVar[PipelineName] = PipelineName.ENV_SETUP
    requires_bootstrap: ClassVar[bool] = True
    experimental: ClassVar[bool] = True
    supported_languages: ClassVar[frozenset[LanguageHint] | None] = None
    required_capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.REMOTE_CLONE})

    def __init__(self, input: GenerationInput, options: BaseModel, bootstrap: Any = None) -> None:
        if Capability.REMOTE_CLONE not in capabilities_for(input.repo.source_kind):
            raise ValueError(
                f"pipeline 'env_setup' requires Capability.REMOTE_CLONE, which "
                f"source kind '{input.repo.source_kind}' does not provide: the "
                f"emitted Dockerfile must be able to clone the repo, and a "
                f"local path is not reachable from a `docker build`."
            )
        if input.llm is None:
            raise ValueError("env_setup requires --llm: recipe distillation is an LLM call")
        self.input = input
        self.options: EnvSetupOptions = options  # type: ignore[assignment]
        self.bootstrap = bootstrap
        self._progress_cb = None

    def set_progress_callback(self, cb) -> None:
        self._progress_cb = cb

    def _emit_progress(self, name: str, outcome: str, reason: str = "") -> None:
        if self._progress_cb is not None:
            try:
                self._progress_cb(name=name, outcome=outcome, reason=reason)
            except Exception as exc:
                logger.debug("progress callback failed: %s", exc)
