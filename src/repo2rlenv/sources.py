"""Input-source contract: where a repo comes from and what it can provide.

Repo2RLEnv historically assumed every input was a GitHub repo. This module
abstracts the *source* so pipelines can run against GitHub, GitLab, or a
local checkout. The key idea is **capabilities**: a source declares what
platform data it can serve (pull requests, issues, arbitrary-commit API),
and a pipeline declares what it requires. `cmd_generate` gates on the
difference and fails fast with a clear message — so e.g. `pr_diff` (needs
pull requests) is blocked on a local repo before any work starts.

Backwards compatibility: a bare ``owner/name`` or a github.com URL resolves
to ``SourceKind.GITHUB`` with the full capability set, exactly as before.

Git clone + commit history + source files are baseline — every source has
them — so the git/source-based pipelines (`commit_runtime`, `code_instruct`,
`equivalence_tests`) work on any source. PR/issue/commit-API data is what
varies, and that's what `Capability` tracks.
"""

from __future__ import annotations

from enum import StrEnum


class Capability(StrEnum):
    """Platform data a source can serve beyond plain git."""

    PULL_REQUESTS = "pull_requests"  # list/fetch merged PRs (pr_diff, pr_runtime)
    ISSUES = "issues"  # fetch issue text by number
    COMMIT_API = "commit_api"  # fetch arbitrary commit diff/parent via host API (cve_patches)


class SourceKind(StrEnum):
    GITHUB = "github"
    GITLAB = "gitlab"
    LOCAL = "local"


# Baseline (clone + git log + source files) is implicit for every source.
# Only the platform-API extras are listed here.
_CAPABILITIES: dict[SourceKind, frozenset[Capability]] = {
    SourceKind.GITHUB: frozenset(
        {Capability.PULL_REQUESTS, Capability.ISSUES, Capability.COMMIT_API}
    ),
    # GitLab clone + git work today; MR/issue mining is tracked in #62.
    SourceKind.GITLAB: frozenset(),
    # A local checkout has no platform layer at all — git only.
    SourceKind.LOCAL: frozenset(),
}


def detect_source_kind(url: str) -> SourceKind:
    """Classify a (normalized) repo URL into a source kind.

    `RepoSpec.normalize_url` canonicalizes local paths to ``file://`` and
    leaves github/gitlab URLs intact, so detection is a simple prefix/host
    check. A bare ``owner/name`` has already been expanded to a github URL.
    """
    u = url.strip()
    if u.startswith("file://"):
        return SourceKind.LOCAL
    if "gitlab.com" in u or u.startswith("git@gitlab.com"):
        return SourceKind.GITLAB
    return SourceKind.GITHUB


def capabilities_for(kind: SourceKind) -> frozenset[Capability]:
    return _CAPABILITIES[kind]
