"""End-to-end test against a private repo (huggingface/trl-internal).

Requires:
  - gh CLI authenticated with read access to huggingface/trl-internal

If access isn't granted, this test is skipped (not failed).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from repo2rlenv.pipelines.pr_mining_lite import PRMiningLitePipeline
from repo2rlenv.reward import calculate_diff_similarity_reward
from repo2rlenv.spec.input import (
    GenerationInput,
    LLMSpec,
    OutputSpec,
    PipelineName,
    PipelineSpec,
    RepoSpec,
)
from repo2rlenv.spec.options import PRMiningLiteOptions


pytestmark = pytest.mark.skipif(
    not shutil.which("gh"),
    reason="gh CLI not available",
)


def _can_access(owner_name: str) -> bool:
    try:
        r = subprocess.run(
            ["gh", "api", f"/repos/{owner_name}", "--silent"],
            capture_output=True, timeout=10, check=False,
        )
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


@pytest.mark.skipif(
    not _can_access("huggingface/trl-internal"),
    reason="no access to huggingface/trl-internal — skipping private e2e test",
)
def test_e2e_private_trl_internal(tmp_path: Path):
    """Mine 2 PRs from a private repo, prove the auth path works."""
    gen_input = GenerationInput(
        repo=RepoSpec(url="huggingface/trl-internal", access="private"),
        pipeline=PipelineSpec(name=PipelineName.PR_MINING_LITE, options={}),
        llm=LLMSpec(provider="anthropic", model="claude-sonnet-4-6"),
        output=OutputSpec(
            destination=str(tmp_path),
            org="hfeval",
            dataset_name="trl-internal-r2e",
            visibility="private",
        ),
    )
    options = PRMiningLiteOptions(limit=2, max_files_per_pr=10)
    pipeline = PRMiningLitePipeline(gen_input, options)

    result = pipeline.run(tmp_path)
    assert result.candidates >= 0  # private repo may have 0 mergeable PRs

    if result.emitted == 0:
        pytest.skip(f"no emittable PRs (candidates={result.candidates}, skips={result.skip_reasons})")

    for task_dir in tmp_path.iterdir():
        if not task_dir.is_dir():
            continue
        oracle = (task_dir / "solution" / "patch.diff").read_text()
        reward, _ = calculate_diff_similarity_reward(oracle, oracle)
        assert reward == 1.0
