"""Verify separately generated initial-state tests on a fresh task before checking completion."""

from __future__ import annotations

import ast

from repo2rlenv.execution.harbor import run_trial
from repo2rlenv.pipelines.recipes.terminal.draft import TerminalDraft, emit_draft
from repo2rlenv.pipelines.recipes.terminal.runner import feedback_for


def initial_state(
    *,
    worker,
    draft,
    design,
    candidate,
    name,
    recipe,
    lineage,
    org,
    timeout_sec,
    resume,
    python,
    trial_id,
    attempt,
    agent_user,
):
    tests = [
        node.name
        for node in ast.parse(design.initial_tests).body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    ]
    data = draft.model_dump()
    data.update(
        tests_python=design.initial_tests,
        weights=[{"name": name, "weight": 1 / len(tests)} for name in tests],
    )
    check = TerminalDraft.model_validate(data)
    task = emit_draft(
        check,
        candidate / f"initial-{attempt}",
        name=name + "-initial",
        org=org,
        recipe=recipe,
        lineage=lineage,
        timeout_sec=timeout_sec,
        resume=resume,
        agent_user=agent_user,
    )
    result = run_trial(
        worker,
        task,
        candidate / f"initial-trial-{attempt}",
        trial_id=trial_id,
        agent="nop",
        python=python,
        resume=resume,
    )
    if result.completed and result.reward == 1:
        return None
    feedback = feedback_for(result, agent="oracle")
    feedback.update(
        agent="initial_state",
        required_repair="The fresh image does not satisfy the initial-state tests. "
        "Repair the setup fixtures; the solver must begin with the inputs described in the task.",
    )
    return feedback
