"""Bootstrap phase — make a repo build cleanly inside a Docker container.

An LLM agent iterates: run a shell command in a container, observe stdout,
think, repeat. When the agent can build + smoke-test the repo, we commit the
container to a Docker image and cache its digest. All future runs of any
sandbox-required pipeline reuse the cached image.

Public API:
    ensure_bootstrap(repo, spec, llm) -> BootstrapResult

See docs/BOOTSTRAP.md for the full design.

----------------------------------------------------------------------------
Acknowledgment
----------------------------------------------------------------------------
The "LLM agent iterates shell commands inside a long-lived container, then
commits to an image" pattern is the same approach used by:

  RepoLaunch (Microsoft / SWE-bench-Live, NeurIPS '25)
  https://github.com/microsoft/RepoLaunch    (MIT)

This module is an INDEPENDENT IMPLEMENTATION of that pattern — no code is
copied from RepoLaunch. We use a standard ReAct-style Thought/Action/Input
agent loop with our own prompts, tool surface, and Docker primitives. The
upstream MIT license does not apply to this file; Repo2RLEnv is Apache-2.0.
----------------------------------------------------------------------------
"""

from repo2rlenv.bootstrap.spec import BootstrapResult, LanguageHint
from repo2rlenv.bootstrap.runner import ensure_bootstrap

__all__ = ["BootstrapResult", "LanguageHint", "ensure_bootstrap"]
