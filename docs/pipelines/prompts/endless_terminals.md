# Endless Terminals: complete prompt reference

Read the [pipeline walkthrough](../endless_terminals.md) first. This reference contains the exact retained templates and the owned code that adds runtime instructions, substitutes variables, builds user messages and selects output schemas. Templates alone are not the final request.

The configured `llm` is used at each model call; roles do not imply different models. Resolved requests are stored as `*.request.json` beside model receipts in the campaign, outside learner-visible bundles. See the [prompt and evidence guide](../prompt_reference.md).

Also read the [shared terminal additions and schemas](shared_terminal.md). They are part of the request where the walkthrough indicates the common builder.

## Retained templates and examples

### environment_prompt.md

[Source: `src/repo2rlenv/pipelines/recipes/endless_terminals/environment_prompt.md`](https://github.com/huggingface/Repo2RLEnv/blob/codex/owned-generation-pipelines/src/repo2rlenv/pipelines/recipes/endless_terminals/environment_prompt.md) · SHA-256 `8ff97082def266bae234be61fda123378951fd6cea3ebfceb2d1d69685f9c750`

Source hash covers the original file; trailing whitespace is omitted below.

<details class="example" markdown="1">
<summary>Read environment_prompt.md</summary>

````text
 You are an expert in Apptainer/Singularity.
You are given a task description and will be tested so that the initial state of the container is set up in a way that an agent can be tested on the task.
Make sure that the container is set up in a way that an agent can be tested on the task.
Basically ensure that the task is valid when the container is built: Clone a repository, create a file, create a directory, create a process, etc.
Install pytest in the container.
Don't include the tests in the response (no %test)
The agent will not have root access. So make sure that the right permissions are set for the files and directories.
Always use this image: docker://ubuntu:22.04
To add it to the def file, use:
Bootstrap: localimage
From: ./ubuntu_22.04.sif
````

</details>

### final_prompt.md

[Source: `src/repo2rlenv/pipelines/recipes/endless_terminals/final_prompt.md`](https://github.com/huggingface/Repo2RLEnv/blob/codex/owned-generation-pipelines/src/repo2rlenv/pipelines/recipes/endless_terminals/final_prompt.md) · SHA-256 `5740eb80e8f1081501e7e5416ee8957a62be86a4687906b0ad708e547a3371ae`

Source hash covers the original file; trailing whitespace is omitted below.

<details class="example" markdown="1">
<summary>Read final_prompt.md</summary>

````text
You are a senior Python engineer who writes robust pytest suites.
Write a robust pytest suite that validates the **FINAL** state of the operating-system / container **after** the student has
completed the task described.
Use the privileged *truth* data to assert the exact expected end state for the task to be completed.

Rules:
* The filename must be ``test_final_state.py`` (show it in a header comment).
* Use **only** the Python standard library and ``pytest`` (no third-party libs).
* Failures must clearly explain **what is still wrong**.
* When you check for files or directories, always use their *absolute* paths exactly as given (no relative paths).
* Ensure that the the state of the OS matches the truth after the task is completed.
* Write the code in a fenced code block that can be parsed to get a single python file.
````

</details>

### initial_prompt.md

[Source: `src/repo2rlenv/pipelines/recipes/endless_terminals/initial_prompt.md`](https://github.com/huggingface/Repo2RLEnv/blob/codex/owned-generation-pipelines/src/repo2rlenv/pipelines/recipes/endless_terminals/initial_prompt.md) · SHA-256 `401cdd63cfc8a794235755e08d0038e9bf54fab23d797755d91f38498ef4feb4`

Source hash covers the original file; trailing whitespace is omitted below.

<details class="example" markdown="1">
<summary>Read initial_prompt.md</summary>

````text

You are a senior Python engineer who writes robust pytest suites.
Write *one* pytest file that validates the operating system / filesystem **before** the student performs the action.
The truth value indicates the answer that the student should get.
You should test for the presence of files, directories, processes, repositories, websites, etc.

Rules:
* The filename should be `test_initial_state.py` (show it in a header comment).
* Use only stdlib + pytest.
* Failures must clearly explain what is missing.
* Ensure that the the state of the OS matches the truth.
* Write the code in a fenced code block that can be parsed to get a single python file.
* When you test for a file or directory, test for the full path to the file or directory, not just relative path.
* DO NOT test for any of the output files or directories.
* The home path is /home/user.
````

</details>

### template_prompt.md

[Source: `src/repo2rlenv/pipelines/recipes/endless_terminals/template_prompt.md`](https://github.com/huggingface/Repo2RLEnv/blob/codex/owned-generation-pipelines/src/repo2rlenv/pipelines/recipes/endless_terminals/template_prompt.md) · SHA-256 `86c9d8f4d7f5ecbc302fbf79fefaf7839d7a84969b98c19661567e84d8b3e18b`

Source hash covers the original file; trailing whitespace is omitted below.

<details class="example" markdown="1">
<summary>Read template_prompt.md</summary>

````text
You are creating realistic Linux-terminal tasks for training an AI agent.

Respond in xml format.

<task>
        Be detailed here. Give the names of the precise contents of files, ports, directories, etc.
        This should be a very detailed description of the final state of the system.
        For example, if you are asking the agent to create a log file, you should precisely specify the format it should be in so that an automated test can verify it.
        Ask the agent to create a log file whenever some verification is required.
        You only have about 1000-1500 words to work with. So balance between conciseness and detail.
        DO NOT directly give the commands to the agent.
</task>

<truth>
        Insert *privileged* ground-truth data that automated
        test suites will rely on to verify correct task execution.
        These values **must NOT** appear in the public task
        description.

        Be very detailed here. Give the names / placeholders of the precise contents of files, ports, directories, repositories, websites etc.
        This should be a very detailed description of the final state of the system.
        For example, if you are asking the agent to create a log file, you should give the name of the log file and the contents of the log file.
        Any processes, files, directories that should be created before the task starts should be mentioned here.
        Any files that should be created by the agent and their contents should be mentioned here.
</truth>

Guidelines:
* Place any secret, ground-truth verification data exclusively under the <truth> element.
* The agent should be able to write to the file and directory that are mentioned in the task description.
* The agent will not have root access. So make sure that the right permissions are set for the files and directories.
* When you mention a file or directory, write the full path to the file or directory, not just relative path.
* The task must be a realistic end-to-end scenario that an AI agent could perform in a Linux terminal.
* Write the task description in a way that a user might ask an AI assistant.
* Be very specific about the names, paths and contents of the files and directories.
* We will be using apptainer to run the agent. So make sure that the task is valid when the container is built.
* Don't create tasks that require having the latest information.
* The home path is /home/user.
* Don't create tasks the setup of which will require su access.
* The task is multi-turn, so the agent will interact in a terminal to finish the task.
* Don't discourage the agent from using console output to finish the task.
* Do not put a constraint on the number of commands that the agent can use (the complixity that the user provides is for the complexity of the task description).
````

</details>

## Request assembly and output contract

The source excerpts below are read-only documentation. Model calls return structured JSON; code in the response executes only in the remote stages shown in the walkthrough.

### recipe.py

[Source: `src/repo2rlenv/pipelines/recipes/endless_terminals/recipe.py`](https://github.com/huggingface/Repo2RLEnv/blob/codex/owned-generation-pipelines/src/repo2rlenv/pipelines/recipes/endless_terminals/recipe.py) · SHA-256 `30bc9bce602e5d4aa16d6fbe0ef38e7d1f21b5df74b1ebd7a26b1e5fb4ce4d30`

Source hash covers the original file; trailing whitespace is omitted below.

<details class="example" markdown="1">
<summary>Read recipe.py</summary>

````python
"""Retained Endless Terminals prompts and native template/test/environment order."""

from __future__ import annotations

from importlib.resources import files

from repo2rlenv.pipelines.recipes.terminal import templates


def design(seed, **kwargs):
    resources = files(__package__)
    return templates.design(
        seed,
        template_prompt=resources.joinpath("template_prompt.md").read_text(),
        initial_prompt=resources.joinpath("initial_prompt.md").read_text(),
        final_prompt=resources.joinpath("final_prompt.md").read_text(),
        capabilities=[seed["category"]],
        **kwargs,
    )


def builder_prompt() -> str:
    return templates.builder_prompt(
        files(__package__).joinpath("environment_prompt.md").read_text()
    )
````

</details>

### sampler.py

[Source: `src/repo2rlenv/pipelines/recipes/endless_terminals/sampler.py`](https://github.com/huggingface/Repo2RLEnv/blob/codex/owned-generation-pipelines/src/repo2rlenv/pipelines/recipes/endless_terminals/sampler.py) · SHA-256 `b3c324a90eb52ddbbbff09d653a3dd39e8146f6b6c677e0df8068812dc847016`

Source hash covers the original file; trailing whitespace is omitted below.

<details class="example" markdown="1">
<summary>Read sampler.py</summary>

````python
"""Endless Terminals' native independent category, complexity and context draws."""

from __future__ import annotations

import json
import random
from importlib.resources import files
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class SamplerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = Field(default=40, ge=1, le=1000)
    seed: int = 24
    categories: list[str] | None = None


def sample_inputs(path: Path) -> list[dict]:
    settings = SamplerInput.model_validate_json(path.read_text())
    taxonomy = json.loads(files(__package__).joinpath("taxonomy.json").read_text())
    categories = taxonomy["TASK_CATEGORIES"]
    if settings.categories is not None:
        if not settings.categories or not set(settings.categories) <= set(categories):
            raise ValueError("Unknown or empty Endless Terminals category selection")
        categories = [item for item in categories if item in settings.categories]
    rng = random.Random(settings.seed)
    records = []
    for index in range(settings.count):
        category = rng.choice(categories)
        complexity = rng.choice(taxonomy["COMPLEXITY_LEVELS"])
        scenario = rng.choice(taxonomy["SCENARIO_CONTEXTS"])
        records.append(
            {
                "source": "endless_terminals_taxonomy",
                "title": f"{category} ({index})",
                "category": category,
                "task_complexity": complexity,
                "scenario": scenario,
                "sampler_seed": settings.seed,
                "draw": index,
                "request": f"Write a new task focusing on {category}. Complexity: {complexity}. "
                f"Scenario: {scenario}. Be very specific about the output format in the task "
                "description that the automated test will check. Write the task description "
                "in a way that a user might ask an AI assistant. The task should be a realistic "
                "end-to-end scenario that an AI agent could perform in a Linux terminal.",
            }
        )
    return records
````

</details>
