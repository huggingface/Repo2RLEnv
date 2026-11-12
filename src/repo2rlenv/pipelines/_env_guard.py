"""Anti-contamination egress guard for sandbox-verified tasks.

A published fix lives on the package index and the code host, so an agent with
open network egress can fetch the gold patch (and often the hidden test) for the
very repo it is being asked to fix. We saw this empirically: an agent blocked
from the web fell back to `git diff origin/main` (closed by the git-history
scrub in `build_environment_dockerfile`), and when that was closed it ran
`pip download <pkg>==<fixed>` and read the fix straight out of the wheel.

This module ships a per-task `environment/docker-compose.yaml` overlay that
Harbor merges into its compose chain. It blackholes the fix-bearing hosts
(PyPI + the repo's own forge and their CDNs) by mapping them to 0.0.0.0, so `pip download`,
`git fetch github.com`, `curl raw.githubusercontent...` and WebFetch against
those hosts all fail, while general internet (the model API, the agent's own
installer, apt) stays reachable.

The same hole exists in every ecosystem whose registry serves source: Go's
module proxy mirrors the repository, and crates.io serves the crate tarball.
For those languages the overlay therefore also blackholes the registry hosts
and sets the toolchain's offline switches, so a cache miss fails fast and
legibly ("not in cache") instead of timing out inside the tool.

This is a *denylist*, which is the realistic control at the docker-compose
layer (`extra_hosts` can add host->ip pins but cannot default-deny). It closes
the obvious, observed leak paths and, crucially, keeps a hosted agent like
claude-code runnable (a full `allow_internet=false` block breaks the agent's
own install + API access). Hosts entries and offline variables are defence in
depth, not isolation: a client that dials an IP literal, or a mirror that is
not on the list, still gets through. The stricter, more robust form is a default-deny
egress allowlist proxy (allow only the package manager + model API), or a
PyPI mirror frozen to the task's base date so even the index lacks the fix.
See docs/concepts/tasks.mdx (contamination defenses).
"""

from __future__ import annotations

from repo2rlenv.bootstrap.spec import LanguageHint
from repo2rlenv.sources import SourceKind

# Hosts that serve the published fix / hidden tests for an open-source repo.
# Blackholed to 0.0.0.0 in the agent's container at run time.
FIX_SOURCE_HOSTS: tuple[str, ...] = (
    "pypi.org",
    "pypi.python.org",
    "files.pythonhosted.org",
    "github.com",
    "api.github.com",
    "raw.githubusercontent.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
)

# gitlab.com serves the merged MR, its diff and the fixed tarball, exactly as
# github.com does. Self-hosted instances are out of scope (see gitlab.py).
GITLAB_FIX_SOURCE_HOSTS: tuple[str, ...] = (
    "gitlab.com",
    "www.gitlab.com",
)

# proxy.golang.org is the sharpest case in any ecosystem: it is a full mirror of
# the module source, so `go mod download <repo>@<version after the fix>` returns
# the answer even when github.com is blackholed. sum.golang.org is on the list
# because the checksum database is how the toolchain validates what it fetches.
GO_FIX_SOURCE_HOSTS: tuple[str, ...] = (
    "proxy.golang.org",
    "sum.golang.org",
)

# static.crates.io serves the published `.crate` tarball (crate source);
# index.crates.io is the sparse registry index cargo resolves versions from.
RUST_FIX_SOURCE_HOSTS: tuple[str, ...] = (
    "crates.io",
    "static.crates.io",
    "index.crates.io",
)

# Offline switches for the same overlay. With the registry hosts blackholed, a
# cache miss otherwise surfaces as a connection error from deep inside the
# tool; these make it say "not in cache" instead. Dependencies are already in
# the bootstrap image (installed via `rebuild_cmds`), and `_GIT_CLEAN_EXCLUDES`
# in pr_runtime keeps `vendor/` and `target/` across the verifier's reset, so a
# correct task never needs the network here.
#
# GOFLAGS is deliberately NOT set. `-mod=mod` would discard a repo's vendor/
# tree and force a proxy fetch, which is the opposite of what we want; leaving
# it alone keeps Go's default (vendor/ wins when vendor/modules.txt exists).
# GOTOOLCHAIN is deliberately not pinned either: a bootstrap image may hold a
# toolchain the go.mod asks for, and `local` would reject it.
GO_OFFLINE_ENV: dict[str, str] = {"GOPROXY": "off", "GOSUMDB": "off"}
RUST_OFFLINE_ENV: dict[str, str] = {"CARGO_NET_OFFLINE": "true"}


def _language(value: LanguageHint | str | None) -> str:
    """Normalise a language given as the enum, its value, or nothing."""
    if value is None:
        return ""
    return str(getattr(value, "value", value)).strip().lower()


def fix_source_hosts(
    source_kind: SourceKind | None = None,
    language: LanguageHint | str | None = None,
) -> tuple[str, ...]:
    """Return the hosts to blackhole for a task mined from `source_kind`.

    The base list covers PyPI and GitHub. A GitLab-sourced repo's fix lives on
    gitlab.com, so a task built from one has to blackhole that too, or the
    agent can simply clone the repo and read the answer.

    `language` adds the ecosystems that serve source under a different host.
    Only Go and Rust are covered: npm is left alone on purpose, because several
    installed agents fetch themselves from registry.npmjs.org at run time, and
    denying one package there while allowing the rest needs an egress proxy
    rather than `extra_hosts`.
    """
    hosts = list(FIX_SOURCE_HOSTS)
    if source_kind is SourceKind.GITLAB:
        hosts += GITLAB_FIX_SOURCE_HOSTS
    language_name = _language(language)
    if language_name == LanguageHint.GO.value:
        hosts += GO_FIX_SOURCE_HOSTS
    elif language_name == LanguageHint.RUST.value:
        hosts += RUST_FIX_SOURCE_HOSTS
    # dict.fromkeys keeps first-seen order while dropping any duplicate.
    return tuple(dict.fromkeys(hosts))


def offline_env(language: LanguageHint | str | None = None) -> dict[str, str]:
    """Environment for the overlay that makes a cache miss fail fast.

    Empty for languages whose registry we do not block, so their emitted task
    is unchanged.
    """
    language_name = _language(language)
    if language_name == LanguageHint.GO.value:
        return dict(GO_OFFLINE_ENV)
    if language_name == LanguageHint.RUST.value:
        return dict(RUST_OFFLINE_ENV)
    return {}


def git_history_scrub(base_commit: str) -> str:
    """Return Dockerfile RUN lines that strip the repo to `base_commit`.

    The working tree may be reset to the base commit, but `.git` still holds
    the *future*: `origin/main`, tags, the fix commit, and the hidden test. An
    agent can `git diff origin/main HEAD` or `git show origin/main:<testfile>`
    to read the answer with zero network. This is a documented, repeated
    incident on SWE-bench (`git log --all | grep`). We remove the remote,
    delete every ref that points past the base, expire the reflog, and gc so
    the future commits are pruned, while keeping `base_commit` itself reachable
    (the verifier resets test files via `git checkout <base_commit> -- ...`).

    A partial clone (`--filter=blob:none`, as `pr_diff` uses) keeps every
    fetched object in promisor packs, which `gc` repacks whether or not anything
    still reaches them. So when promisor packs are present, the last line
    rewrites the object store to what the remaining refs reach.

    Append these lines AFTER the `git reset --hard <base_commit>` line.
    """
    return (
        f"RUN git checkout -q -B base {base_commit} \\\n"
        f"    && git remote remove origin 2>/dev/null || true\n"
        f"RUN for ref in $(git branch --format='%(refname:short)' | grep -vx base); do "
        f'git branch -D "$ref" 2>/dev/null || true; done \\\n'
        f"    && git tag -l | xargs -r git tag -d >/dev/null 2>&1 || true \\\n"
        f"    && git for-each-ref --format='%(refname)' refs/remotes 2>/dev/null "
        f"| xargs -r -n1 git update-ref -d 2>/dev/null || true \\\n"
        f"    && git reflog expire --expire=now --all 2>/dev/null || true \\\n"
        f"    && git gc --prune=now 2>/dev/null || true\n"
        # Each step must succeed before the old packs go: a partial object
        # list would leave the base commit unreadable.
        'RUN P="$(git rev-parse --git-dir)/objects/pack"; '
        'if ls "$P"/*.promisor >/dev/null 2>&1; then \\\n'
        '        git rev-list --objects --all --missing=allow-promisor > "$P/../scrub.list" \\\n'
        '        && K=$(git pack-objects -q --missing=allow-promisor "$P/scrub" < "$P/../scrub.list") \\\n'
        '        && rm -f "$P/../scrub.list" "$P"/pack-* "$P"/multi-pack-index* \\\n'
        '        && touch "$P/scrub-$K.promisor" && git prune --expire=now; \\\n'
        "    fi\n"
    )


def egress_guard_compose(
    hosts: tuple[str, ...] = FIX_SOURCE_HOSTS,
    language: LanguageHint | str | None = None,
) -> str:
    """Return `environment/docker-compose.yaml` content blackholing `hosts`.

    Harbor includes a task's `environment/docker-compose.yaml` in its compose
    `-f` chain, so the `extra_hosts` entries land on the agent's `main` service.

    `language` adds that ecosystem's offline switches (see `offline_env`) to the
    same service, so a dependency that is not already in the image fails with
    "not in cache" rather than a connection error from inside the tool. For
    languages with no switches the overlay is exactly what it was before.
    """
    lines = [
        "# Auto-generated by Repo2RLEnv: anti-contamination egress guard.",
        "# Blackholes the hosts that serve this repo's published fix and hidden",
        "# tests, so an agent cannot fetch the answer (pip download / git fetch /",
        "# WebFetch against these fail). General internet stays up so the agent",
        "# can install itself and reach its model API. See pipelines/_env_guard.py.",
        "# Denylist plus offline switches: defence in depth, not isolation.",
        "services:",
        "  main:",
        "    extra_hosts:",
    ]
    lines += [f'      - "{h}:0.0.0.0"' for h in hosts]
    env = offline_env(language)
    if env:
        lines.append("    environment:")
        lines += [f'      {key}: "{value}"' for key, value in env.items()]
    return "\n".join(lines) + "\n"
