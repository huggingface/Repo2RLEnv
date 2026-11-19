"""Generate a missing PR regression test without executing model-authored shell."""

from __future__ import annotations

import json
import logging
import re
import shlex
from pathlib import PurePosixPath

from repo2rlenv.bootstrap.docker import DockerSandbox
from repo2rlenv.github import PullRequestSummary
from repo2rlenv.pipelines.code_instruct import new_file_hunk
from repo2rlenv.pipelines.pr_runtime import _files_in_patch, _path_is_test
from repo2rlenv.spec.input import LLMSpec

logger = logging.getLogger(__name__)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SAFE_TEST_PATH_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
_MAX_TEST_BYTES = 32_768
_TEST_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
    "required": ["path", "content"],
    "additionalProperties": False,
}

_SYSTEM = """You write one regression test for a real bug fix.

Return only a JSON object with exactly two string fields: `path` and `content`.
The path must be a new test-file path in the repository's established test layout.
The content must be a deterministic test using the repository's existing test
framework and conventions. The test must fail on the supplied base commit because
of the reported bug and pass after the supplied source fix is applied.

Do not write production changes, modify existing tests, use network access, or
include markdown fences or prose. Keep the test focused on the reported behavior.
"""


def _base_context(
    sandbox: DockerSandbox, *, base_commit: str, source_patch: str
) -> tuple[str, str, set[str]] | None:
    """Return bounded changed-source and existing-test context from the base tree."""
    if not _SHA_RE.fullmatch(base_commit):
        return None
    from repo2rlenv.pipelines.pr_runtime_validate import _fetch_base_commit

    if not _fetch_base_commit(sandbox, base_commit):
        logger.info("test synthesis could not fetch base commit %s", base_commit[:12])
        return None

    def show(path: str, limit: int) -> str:
        result = sandbox.exec(
            f"git -C /workspace show {shlex.quote(f'{base_commit}:{path}')} | head -c {limit}",
            timeout=30,
        )
        return result.stdout if result.ok else ""

    source_parts = []
    for path in _files_in_patch(source_patch)[:3]:
        content = show(path, 5000)
        if content:
            source_parts.append(f"--- {path} (base commit) ---\n{content}")

    tree = sandbox.exec(
        f"git -C /workspace ls-tree -r --name-only {shlex.quote(base_commit)}", timeout=60
    )
    paths = set(tree.stdout.splitlines()) if tree.ok else set()
    test_paths = sorted(path for path in paths if _path_is_test(path))[:12]
    test_parts = []
    for path in test_paths[:3]:
        content = show(path, 2500)
        if content:
            test_parts.append(f"--- {path} ---\n{content}")
    return "\n".join(source_parts), "\n".join(test_parts), paths


def _test_file_diff(response: str, *, existing_paths: set[str]) -> str | None:
    """Validate model output as a safe new test file and wrap it as a git patch."""
    response = response.strip()
    if response.startswith("```json") and response.endswith("```"):
        response = response[7:-3].strip()
    elif response.startswith("```") and response.endswith("```"):
        response = response[3:-3].strip()
    try:
        result = json.loads(response)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(result, dict) or set(result) != {"path", "content"}:
        return None
    path, content = result["path"], result["content"]
    if not isinstance(path, str) or not isinstance(content, str):
        return None
    parsed = PurePosixPath(path)
    if (
        not path
        or not _SAFE_TEST_PATH_RE.fullmatch(path)
        or parsed.is_absolute()
        or parsed.as_posix() != path
        or any(part in {"", ".", ".."} for part in parsed.parts)
        or "\\" in path
        or "\0" in path
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        or path.startswith("-")
        or len(path) > 240
        or not _path_is_test(path)
        or path in existing_paths
        or not content.strip()
        or len(content.encode("utf-8")) > _MAX_TEST_BYTES
    ):
        return None
    return new_file_hunk(content if content.endswith("\n") else content + "\n", path)


def synthesize_test_patch(
    sandbox: DockerSandbox,
    *,
    llm: LLMSpec,
    pr: PullRequestSummary,
    base_commit: str,
    source_patch: str,
    test_cmds: list[str],
    existing_test_patch: str = "",
    feedback: str = "",
) -> tuple[str | None, float]:
    """Ask the configured LLM for a test file; validation remains the caller's job."""
    context = _base_context(sandbox, base_commit=base_commit, source_patch=source_patch)
    if context is None:
        return None, 0.0
    source, existing_tests, existing_paths = context
    from repo2rlenv.llm import complete

    # Limit submitted repository context to the changed files and a few test examples.
    user = (
        f"Problem report:\n{pr.title}\n\n{(pr.body or '')[:5000]}\n\n"
        f"Base commit: {base_commit}\n"
        f"Test commands recorded for this repository: {json.dumps(test_cmds)}\n\n"
        f"Changed source at the base commit:\n{source[:15000]}\n\n"
        f"Existing test examples:\n{existing_tests[:9000]}\n\n"
        f"Source fix diff (use this only to identify the regression):\n{source_patch[:12000]}\n"
        + (f"\nPrevious synthesis feedback:\n{feedback[:2000]}\n" if feedback else "")
    )
    try:
        response = complete(
            llm,
            system=_SYSTEM,
            user=user,
            max_tokens=4096,
            temperature=0.2,
            response_schema=_TEST_RESPONSE_SCHEMA,
        )
    except Exception as exc:
        logger.info("PR test synthesis failed (%s)", type(exc).__name__)
        return None, 0.0
    existing_paths.update(_files_in_patch(existing_test_patch))
    generated = _test_file_diff(response.content or "", existing_paths=existing_paths)
    if generated is None:
        logger.info("PR test synthesis response did not contain a safe, new test file")
    return generated, response.cost_usd
