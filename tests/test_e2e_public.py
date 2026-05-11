"""End-to-end test against a public repo (huggingface/trl).

Requires:
  - gh CLI installed and authenticated (or GITHUB_TOKEN env)
  - network access

If gh isn't available, the test is skipped.
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
    reason="gh CLI not available — skipping end-to-end tests",
)


def _gh_authenticated() -> bool:
    try:
        r = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, timeout=5, check=False
        )
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


@pytest.mark.skipif(not _gh_authenticated(), reason="gh not authenticated")
def test_e2e_public_trl(tmp_path: Path):
    """Mine 2 PRs from huggingface/trl and verify each is a valid Harbor task."""
    gen_input = GenerationInput(
        repo=RepoSpec(url="huggingface/trl", access="public"),
        pipeline=PipelineSpec(name=PipelineName.PR_MINING_LITE, options={}),
        llm=LLMSpec(provider="anthropic", model="claude-sonnet-4-6"),
        output=OutputSpec(
            destination=str(tmp_path),
            org="hfeval",
            dataset_name="trl-r2e",
        ),
    )
    options = PRMiningLiteOptions(limit=2, max_files_per_pr=10)
    pipeline = PRMiningLitePipeline(gen_input, options)

    result = pipeline.run(tmp_path)

    assert result.candidates >= 1, "should have at least one PR candidate"
    assert result.emitted >= 1, f"should emit ≥1 task, got 0 (skips: {result.skip_reasons})"

    # Verify each emitted task
    for task_dir in tmp_path.iterdir():
        if not task_dir.is_dir():
            continue
        assert (task_dir / "task.toml").is_file()
        assert (task_dir / "instruction.md").is_file()
        oracle = task_dir / "solution" / "patch.diff"
        assert oracle.is_file()
        assert oracle.stat().st_size > 0, f"empty oracle in {task_dir}"

        # Self-similarity sanity check
        oracle_text = oracle.read_text()
        reward, _ = calculate_diff_similarity_reward(oracle_text, oracle_text)
        assert reward == 1.0, f"oracle should self-score 1.0 in {task_dir}"
