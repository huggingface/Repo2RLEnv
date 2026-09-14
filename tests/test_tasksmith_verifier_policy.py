"""Catch observed source-blacklist grading without executing target code."""

from __future__ import annotations

import pytest

from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, write_bundle
from repo2rlenv.quality.loop.artifacts import apply_repair, task_identity
from repo2rlenv.quality.loop.models import Edit, Repair
from repo2rlenv.quality.loop.verifier_policy import (
    TASKSMITH_BEHAVIOR_PATH,
    check_behavioral_verifier,
)
from repo2rlenv.tasksmith.models import Design


@pytest.mark.parametrize(
    "source",
    [
        "with open(trainer.__file__) as f:\n    assert re.search('known wrong', f.read()) is None",
        "assert 'wrong' not in Path(trainer.__file__).read_text()",
        "assert Path(trainer.__file__).read_bytes() != b'wrong'",
        "import inspect as i\nassert 'wrong' not in i.getsource(trainer.generate)",
        "from inspect import getsourcelines as lines\nassert lines(trainer.generate)",
    ],
)
def test_known_implementation_source_grading_is_rejected(source):
    with pytest.raises(ValueError, match="reads implementation source at line"):
        check_behavioral_verifier(source)


@pytest.mark.parametrize(
    "source",
    [
        "",
        "# open(trainer.__file__) is not a test\nassert trainer.generate() == ['expected']",
        "from inspect import signature\nassert 'tools' in signature(trainer.generate).parameters",
        "fixture = Path(__file__).with_name('input.json').read_text()\nassert parse(fixture)",
        "fixture = Path(package.__file__).with_name('fixture.json').read_text()",
        "fixture = (Path(package.__file__).parent / 'fixtures' / 'case.json').read_text()",
        "with open(Path(package.__file__).with_name('fixture.json')) as f: assert f.read()",
        "assert 'open(trainer.__file__)' in documentation",
    ],
)
def test_behavior_public_api_and_fixture_reads_are_allowed(source):
    check_behavioral_verifier(source)


def test_design_rejects_bad_source_through_bounded_artifact_validation():
    fields = {
        "instruction": "Implement reusable environments and preserve observable behavior. " * 2,
        "requirements": [
            {
                "behavior": "Environment instances are reused between batches.",
                "verification": "Run consecutive production batches and observe identities.",
                "source_evidence": "trainer.py changes in the source diff",
            }
        ],
        "verifier_rationale": "Production calls expose actual environment reuse.",
        "wrong_solution_ideas": ["Create a new instance for every batch."],
        "valid_alternative_ideas": ["Reuse instances in a different order."],
    }
    for source in ["with open(trainer.__file__) as f: assert f.read()", "def broken(:"]:
        with pytest.raises(ValueError, match="Tasksmith behavioral verifier"):
            Design(**fields, additional_tests=source)


@pytest.mark.parametrize("recipe", ["tasksmith", "other-recipe"])
def test_repair_guard_is_transactional_and_scoped_to_tasksmith(tmp_path, recipe):
    old = "def test_reuse():\n    assert trainer.generate() == ['expected']\n"
    new = "with open(trainer.__file__) as f:\n    assert 'known wrong' not in f.read()\n"
    task = write_bundle(
        TaskBundle(
            name="behavioral-repair",
            org="tests",
            instruction="Implement environment reuse.\n",
            files={
                "environment/Dockerfile": TaskFile.text("FROM python:3.12-slim\n"),
                "solution/solve.sh": TaskFile.text("#!/bin/sh\nexit 0\n", executable=True),
                "tests/test.sh": TaskFile.text("#!/bin/sh\nexit 0\n", executable=True),
                TASKSMITH_BEHAVIOR_PATH: TaskFile.text(old),
            },
            metadata={"recipe": recipe, "recipe_version": "1", "reward_kinds": ["test_execution"]},
        ),
        tmp_path / "tasks",
    )
    before = task_identity(task)
    repair = Repair(
        explanation="Attempt to reject a known source mutation.",
        addressed_issues=["A wrong implementation passed."],
        edits=[Edit(path=TASKSMITH_BEHAVIOR_PATH, old=old, new=new, executable=False)],
    )
    destination = tmp_path / "r1" / task.name
    if recipe == "tasksmith":
        with pytest.raises(ValueError, match="reads implementation source"):
            apply_repair(task, repair, destination)
        assert not destination.exists()
        assert not (destination.parent / "repair.json").exists()
    else:
        apply_repair(task, repair, destination)
        assert (destination / TASKSMITH_BEHAVIOR_PATH).read_text() == new
    assert task_identity(task) == before
    assert (task / TASKSMITH_BEHAVIOR_PATH).read_text() == old
