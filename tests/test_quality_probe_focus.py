"""Distinguish deferred resource construction from lazy generator outputs."""

from pathlib import Path

import pytest

from repo2rlenv.quality.loop.runner import required_probe_focus


def requirement_task(tmp_path: Path, instruction: str, metadata: str = "") -> Path:
    (tmp_path / "instruction.md").write_text(instruction)
    (tmp_path / "task.toml").write_text(metadata)
    return tmp_path


@pytest.mark.parametrize(
    "instruction",
    [
        "Environments are created lazily and reused across generation batches.",
        "Use lazy environment pooling and bound construction by peak concurrency.",
        "Initialize the model lazily on first use.",
        "Populate a lazy cache only when needed.",
        "Open the database connection lazily.",
        "Read input integers supplied by generators.",
    ],
)
def test_resource_laziness_does_not_require_generator_probe(tmp_path, instruction):
    assert required_probe_focus(requirement_task(tmp_path, instruction)) == set()


@pytest.mark.parametrize(
    "instruction",
    [
        "Yield lazily. Write the sum.",
        "Lazily yield each result.",
        "Return a generator rather than materializing all results.",
        "The API returns a generator.",
        "Implement a generator function.",
        "Return lazy output.",
        "Use a lazy iterator over values.",
        "Produce the output values lazily.",
    ],
)
def test_generator_output_contract_keeps_required_probe(tmp_path, instruction):
    assert required_probe_focus(requirement_task(tmp_path, instruction)) == {"lazy_output"}


def test_explicit_requirements_survive_unrelated_lazy_wording(tmp_path):
    task = requirement_task(
        tmp_path,
        "Create environments lazily with a relative error bound.",
        '[metadata.repo2env.quality_requirements]\n'
        'probe_focus = ["lazy_output", "model_behavior"]\n',
    )
    assert required_probe_focus(task) == {"lazy_output", "model_behavior", "numeric_tolerance"}
