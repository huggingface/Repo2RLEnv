# Contributing to Repo2RLEnv

Thanks for your interest in contributing. This is a small research project; the bar for changes is "does it improve the synthesis path or make tasks more verifiable?" — not "does it match an exact roadmap." Small PRs are welcome; large refactors should start with an issue first.

## Quick start (dev environment)

```bash
# Clone + enter
git clone https://github.com/huggingface/Repo2RLEnv.git
cd Repo2RLEnv

# uv handles Python + venv + deps in one step
uv sync --group dev          # installs runtime + dev deps (pytest, ruff)

# Sanity check
uv run pytest -q             # should print 620/620 passing (or higher)
uv run ruff check .          # should report "All checks passed!"
uv run ruff format --check . # should report "N files already formatted"
uv run repo2rlenv --version  # 0.3.0 (or higher)
```

Python **3.12 / 3.13 / 3.14** are all supported and CI tests against each. The minimum is `requires-python = ">=3.12"`.

External tools you'll want installed:
- **`gh` CLI** (`brew install gh` then `gh auth login`) — the canonical GitHub auth path for both `pr_diff` and `pr_runtime`
- **Docker** — required for any `_runtime` pipeline + the bootstrap phase. Lite pipelines (`pr_diff`) work without it.
- An **LLM provider key** in your environment — `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc. (any [LiteLLM](https://docs.litellm.ai/docs/providers)-supported provider). Bootstrap needs this; pure `pr_diff` runs don't.

## Issues

Open one **before** starting non-trivial work. Useful issues include:

- A repro: exact command, repo + ref, what you saw vs. what you expected, the last ~30 lines of stderr
- For pipeline ideas: which paper / repo it's inspired by, what oracle it produces, whether it needs a sandbox
- For bugs: a failing test case (or "I tried but couldn't repro" — that's still useful)

Triage labels we use: `pipeline`, `bootstrap`, `bug`, `ui`, `docs`, `infra`. Don't worry about labelling — we'll handle it.

## Pull requests

### Branching

- Cut feature branches from `main`: `feat/<short-slug>` for features, `fix/<short-slug>` for bug fixes, `docs/<short-slug>` for docs-only changes.
- Rebase on `main` before opening the PR; merges should be fast-forward or squash. The merge button on GitHub is configured for squash by default.

### Title + description

- PR title under 70 characters, present-tense, no period: `pr_runtime: filter docs-only test_patches`, not `pr_runtime - filtering docs-only test_patches.`.
- Description has three sections: **Summary** (1-3 bullets), **Test plan** (bulleted checklist of what you ran), and **Out of scope** (anything you deferred).
- If the PR fixes an issue, end the body with `Closes #N`. GitHub will auto-close on merge.

### Commit message conventions

We don't enforce Conventional Commits, but:

- Subject line under ~70 characters, imperative mood ("add foo" not "added foo" or "adds foo")
- Body explains **why**, not what (the diff already shows what)
- No `Co-Authored-By: Claude` trailer or other AI-assistant attribution — the user-side commits land under your own name
- Reference issues in the body, not the subject: `Closes #42` on its own line is fine

Example:

```
pr_runtime: skip CI-only PRs

PRs whose source patch is 100% under .github/ produce non-test
training data — no behavioral change to verify. New
`ci_only_patch` skip reason in PRRuntimeOptions.skip_ci_only
(default True).

Closes #N
```

### What CI checks on every PR

- **lint** — `uv run ruff check .` + `uv run ruff format --check .`
- **test** — `uv run pytest -q` against Python 3.12, 3.13, 3.14 (matrix)
- **build** — `uv build` produces sdist + wheel, smoke-installs the wheel, checks `repo2rlenv --version`

A green CI is the floor for merge — green plus at least one approving review is the ceiling.

### Code style

- **Ruff handles formatting**: 100-char line length, double quotes, modern Python idioms (`X | None` not `Optional[X]`, `list[str]` not `List[str]`, etc.). `ruff format .` fixes everything; CI rejects unformatted code.
- **Ruff handles linting**: rule set is `E,W,F,I,B,UP,SIM,RUF`. Real bugs (`F821`, `F841`, `B904`, `B017`) block CI; stylistic preferences are configured in `[tool.ruff.lint]` under `pyproject.toml`.
- **Type hints**: use them on public functions + dataclasses. Internal helpers can skip them when obvious. We don't run mypy in CI (yet); ruff catches most of what mypy would catch for our shape of code.
- **Imports**: stdlib → third-party → first-party, alphabetized within groups. Ruff's `I` rules auto-sort.
- **`from __future__ import annotations`**: at the top of every module. Lets us use modern type-hint syntax even on the runtime path.

### Dependencies

- **Use `uv add <pkg>`** to add a runtime dep, **`uv add --dev <pkg>`** for dev-only. Never hand-edit `pyproject.toml`'s `dependencies` array — it desyncs from `uv.lock`.
- Prefer stdlib when reasonable. We're a research repo, not a kitchen-sink framework; every dep is a future supply-chain risk and an install-time slowdown.
- If you add a transitive subdependency that's load-bearing for our public API (e.g. exposing a Pydantic v3 type), pin a minimum version in `pyproject.toml` so consumers don't break on older installs.

### Tests

- **Every code change keeps the suite green.** `uv run pytest -q` is the canonical command. Currently 620/620; if you add code, you usually add tests.
- **Unit tests live next to the module they test**: `tests/test_<module>.py`. They use `pytest`'s fixture model; we don't use any custom test runner.
- **E2E tests** (against real GitHub repos) live in `tests/test_e2e_*.py`. They're skipped automatically when `gh` isn't authenticated or the test repo isn't accessible — so they don't break CI for contributors without those credentials.
- Use **specific exception types** in `pytest.raises(...)` — `pytest.raises(ValidationError)`, not `pytest.raises(Exception)`. Ruff flags the latter (`B017`).
- When testing a pipeline, prefer **fixture-based** input over mocking — load a recorded PR diff from a file rather than constructing one programmatically. Easier to read, easier to maintain.

## Adding a new pipeline

That's a structured task with its own walkthrough — see [**`docs/contributing/ADDING_A_PIPELINE.md`**](./docs/contributing/ADDING_A_PIPELINE.md). It covers the enum + Options + Pipeline class + tests + doc page, with template snippets and conventions taken from `pr_diff` / `pr_runtime`.

## Releases

Releases are tag-driven and handled by `.github/workflows/release.yml`. The flow:

1. Bump `version` in `pyproject.toml` (e.g. `0.3.0` → `0.4.0`). The `__version__` and `repo2rlenv --version` output read from package metadata, so no other code change needed.
2. Commit + push to `main`. CI confirms tests still pass.
3. Tag: `git tag v0.4.0 && git push origin v0.4.0`.
4. Create a GitHub Release pointing at the tag: `gh release create v0.4.0 --generate-notes` (or use the web UI).
5. The `Release` workflow auto-fires on publication, runs the test matrix one more time against the tag, builds sdist + wheel, publishes to PyPI via `PYPI_API_TOKEN`, and attaches the dist files to the GitHub Release.

If the publish step fails (auth, network, etc.), you can re-trigger it manually via `gh workflow run Release --ref v0.4.0 -f tag=v0.4.0` without needing to retag. PyPI's `skip-existing: true` flag means a partial-success rerun won't double-publish.

## Acknowledging external work

We've taken inspiration from a constellation of repository-mining and RL-environment projects — SWE-RL, SWE-bench, SWE-bench-Live, SWE-smith, RepoLaunch, Magicoder, R2E, PatchSeeker. If your contribution draws code or algorithms from any external work:

- Add an **Acknowledgment block** at the top of the relevant `.py` file. See `src/repo2rlenv/bootstrap/__init__.py` or `src/repo2rlenv/reward.py` for the format.
- Be explicit about: source repo URL, license of that repo, paper citation if relevant, and our license posture (independent reimplementation, no code copied → Apache-2.0 still applies; vs. derivative work → upstream license terms might).
- We don't vendor external code unless its license is compatible with Apache-2.0 *and* the alternative (reimplementing) is genuinely too costly.

## License

By contributing, you agree that your contributions are licensed under Apache-2.0 (matching the project's [LICENSE](./LICENSE)).

## Where to ask

- **Bug or feature**: open an issue at https://github.com/huggingface/Repo2RLEnv/issues
- **Quick question**: same place — open an issue with the `question` label
- **Security report**: email adithyaskolavi@huggingface.co directly. Don't open a public issue for unfixed security bugs.
