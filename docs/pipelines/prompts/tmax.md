# TMax: complete prompt reference

Read the [pipeline walkthrough](../tmax.md) first. This reference contains the exact retained templates and the owned code that adds runtime instructions, substitutes variables, builds user messages and selects output schemas. Templates alone are not the final request.

The configured `llm` is used at each model call; roles do not imply different models. Resolved requests are stored as `*.request.json` beside model receipts in the campaign, outside learner-visible bundles. See the [prompt and evidence guide](../prompt_reference.md).

Also read the [shared terminal additions and schemas](shared_terminal.md). They are part of the request where the walkthrough indicates the common builder.

## Retained templates and examples

### environment_prompt.md

[Source: `src/repo2rlenv/pipelines/recipes/tmax/environment_prompt.md`](https://github.com/huggingface/Repo2RLEnv/blob/codex/owned-generation-pipelines/src/repo2rlenv/pipelines/recipes/tmax/environment_prompt.md) · SHA-256 `1ba83f0ea6d063304b358a37faadf7000beb94bee341e49a52f3eebc7d870396`

Source hash covers the original file; trailing whitespace is omitted below.

<details class="example" markdown="1">
<summary>Read environment_prompt.md</summary>

````text
You are an expert in Apptainer/Singularity container setup.

You will be given a task description, ground truth, and initial-state tests.
Your job is to write an Apptainer .def file that sets up the initial state
of the container so that an agent can be tested on the task.

IMPORTANT RULES:
- Always start the def file with exactly:
  Bootstrap: docker
  From: ubuntu:22.04
- In the %post section:
  1. Start with: export DEBIAN_FRONTEND=noninteractive
  2. Run: apt-get update && apt-get install -y python3 python3-pip
  3. Run: pip3 install pytest
  4. Install ONLY the additional system or Python packages the task needs.
     Keep package installs minimal. Prefer pip over apt when possible.
  5. Create files, directories, and data needed for the task.
  6. Create the user: useradd -m -s /bin/bash user || true
  7. End with: chmod -R 777 /home/user
- Do NOT include %test sections.
- Do NOT create output files that the agent should produce.
- The home path is /home/user.
- Do NOT override HOME in %environment.
- Do NOT use Apptainer build variables (no {{ }}).
- Do NOT use exotic package names. If you need awk, install gawk.
  The command 'tr' is part of coreutils, not a separate package.
````

</details>

### final_prompt.md

[Source: `src/repo2rlenv/pipelines/recipes/tmax/final_prompt.md`](https://github.com/huggingface/Repo2RLEnv/blob/codex/owned-generation-pipelines/src/repo2rlenv/pipelines/recipes/tmax/final_prompt.md) · SHA-256 `56fdcf342d3b7aecc0818f57cd1127e1ba6ad2b04df485298e3f5743f89b4c7b`

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

Ground-truth alignment (principled tests):
* Treat *truth* as the **intent** of the rubric, not as guaranteed-correct literals. When
  the task and setup logically determine an expected value, **derive or recompute** it in
  test code (stdlib only) instead of copying opaque constants from *truth* without checking.
* Match the **same procedures and ordering** as the described setup and task when you
  assert counts, checksums, or structured outputs—so tests stay faithful to the spec.
* Use the **strongest appropriate** assertion: prefer invariants, structure, and
  reproducible computations over brittle full-file equality when *truth* still allows the
  task to be graded fairly.
````

</details>

### initial_prompt.md

[Source: `src/repo2rlenv/pipelines/recipes/tmax/initial_prompt.md`](https://github.com/huggingface/Repo2RLEnv/blob/codex/owned-generation-pipelines/src/repo2rlenv/pipelines/recipes/tmax/initial_prompt.md) · SHA-256 `35666295b20ec04cb46005da85be205b7019562739971ff57a42daa6254c5e08`

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

[Source: `src/repo2rlenv/pipelines/recipes/tmax/template_prompt.md`](https://github.com/huggingface/Repo2RLEnv/blob/codex/owned-generation-pipelines/src/repo2rlenv/pipelines/recipes/tmax/template_prompt.md) · SHA-256 `f9f1cca0c4d111331bc44554aceada17713fba908c5965665991e6cb1098fc5f`

Source hash covers the original file; trailing whitespace is omitted below.

<details class="example" markdown="1">
<summary>Read template_prompt.md</summary>

````text
You are an expert at creating {{domain_label}} tasks for AI agent training.

{{module}}{{v2_block}}

Universal Task Requirements:
- Challenging to solve: Requires domain knowledge, analytical thinking, and efficient implementation.
- Easy to verify: Success must be determinable by programmatically checking outputs, exit codes, or system state.
- Self-contained: All necessary information must be in the prompt.
- Realistic: The problem should resemble tasks professionals face in this domain.

Respond in XML format using these tags:

<task>
        A detailed task description written as a user would ask an AI assistant.
        Give the names of the precise contents of files, ports, directories, etc.
        This should be a very detailed description of the final state of the system.
        For example, if you are asking the agent to create a log file, you should
        precisely specify the format it should be in so that an automated test can
        verify it.
        Ask the agent to create a log file whenever some verification is required.
        You only have about 1000-1500 words to work with. So balance between
        conciseness and detail.
        DO NOT directly give the commands to the agent.
</task>

<truth>
        Insert *privileged* ground-truth data that automated test suites will
        rely on to verify correct task execution.
        These values **must NOT** appear in the public task description.

        Be very detailed here. Give the names / placeholders of the precise
        contents of files, ports, directories, repositories, websites etc.
        Any processes, files, directories that should be created before the task
        starts should be mentioned here.
        Any files that should be created by the agent and their contents should
        be mentioned here.

        Ground-truth principles (for accurate automated verification):
        * **Consistency:** Anything in *truth* that could be computed from the setup
          code, random seeds, or the task rules must actually follow from them. Do not
          assert derived numbers, digests, or file bodies unless they are implied by
          what you specified—avoid plausible-looking literals produced without that chain.
        * **Reproducibility:** Prefer stating *how* to obtain a golden value (procedure,
          formula, or a short **runnable** snippet that prints the canonical result) over
          pasting opaque constants that nothing in the pipeline verifies.
        * **Causal ordering:** When setup involves multiple steps (random draws, mutations,
          I/O), make the sequence explicit. Headline summaries (e.g. simple counts) must
          reflect the real order of operations, not an informal intuition.
        * **Single source of truth:** Setup scripts, narrative expectations, and any
          “expected output” blocks must agree; resolve contradictions before finishing.
</truth>

Critical Rules:
* No Leakage: Never include code that solves the task in the <task> description.
* Verification: Prioritize tasks with clear, programmatic verification.
* Originality: Tasks should require thought, not just copying standard tutorials.
* Complete Specification: Include all information needed to complete the task (file paths, formats, constraints).
* Place any secret, ground-truth verification data exclusively under <truth>.
* The agent will not have root access. Make sure the right permissions are set for files and directories.
* When you mention a file or directory, write the full path (not relative).
* We will be using apptainer to run the agent. Make sure the task is valid when the container is built.
* Don't create tasks that require having the latest information.
* The home path is /home/user.
* Don't create tasks the setup of which will require su access.
* The task is multi-turn, so the agent will interact in a terminal to finish the task.
* Don't discourage the agent from using console output to finish the task.
* Do not constrain the number of commands the agent may use.
````

</details>

### DOMAIN_MODULES substitutions

[Source: `src/repo2rlenv/pipelines/recipes/tmax/taxonomy.json`](https://github.com/huggingface/Repo2RLEnv/blob/codex/owned-generation-pipelines/src/repo2rlenv/pipelines/recipes/tmax/taxonomy.json) · SHA-256 `fb63e54c2c15e4547030bc7507e234ac19907dd4f2ab772a8256076669a7cc06`

Source hash covers the original file; trailing whitespace is omitted below.

<details class="example" markdown="1">
<summary>Read DOMAIN_MODULES substitutions</summary>

````json
{
  "security": "# Security Task Builder\n\nDomain Focus\nCreate tasks involving:\n- **Cryptography**: Encryption, decryption, key management, hash functions\n- **Vulnerability Analysis**: Code review, exploit identification, security auditing\n- **Authentication**: Password handling, token validation, session management\n- **Network Security**: Protocol analysis, traffic inspection, firewall rules\n- **Secure Coding**: Input validation, output encoding, secure defaults\n\nThe task should test security skills and require security knowledge and analytical thinking.",
  "software_engineering": "# Software Engineering Task Builder\n\nDomain Focus\nCreate tasks involving:\n- **Code quality**: Refactoring, testing, documentation\n- **Build systems**: Compilation, linking, packaging\n- **Version control**: Git operations, merge conflicts, history analysis\n- **API design**: REST, GraphQL, protocol design\n- **Architecture**: Patterns, modularity, scalability\n\nThe task should test software engineering skills and require domain knowledge and analytical thinking.",
  "file_operations": "# File Operations Task Builder\n\nDomain Focus\nCreate tasks involving:\n- **File I/O**: Reading, writing, appending, seeking\n- **Directory operations**: Traversal, creation, permissions\n- **File formats**: Binary, text, structured data\n- **Compression**: Zip, tar, gzip, custom formats\n- **File system operations**: Links, permissions, metadata\n\nThe task should test file operations skills and require domain knowledge and analytical thinking.",
  "data_querying": "# Data Querying Task Builder\n\nDomain Focus\nCreate tasks involving:\n- **SQL operations**: Complex joins, window functions, CTEs\n- **Query optimization**: Indexes, execution plans, performance\n- **Database operations**: Schema design, migrations, constraints\n- **NoSQL patterns**: Document, key-value, graph queries\n- **Data retrieval**: Pagination, filtering, full-text search\n\nThe task should test data querying skills and require domain knowledge and analytical thinking.",
  "data_science": "# Data Science Task Builder\n\nDomain Focus\nCreate tasks involving:\n- **Exploratory Analysis**: Statistical summaries, visualization, pattern discovery\n- **Feature Engineering**: Transformation, encoding, selection, creation\n- **Statistical Modeling**: Regression, hypothesis testing, Bayesian analysis\n- **Data Mining**: Clustering, association rules, anomaly detection\n- **Reporting**: Automated insights, metric computation, summary generation\n\nThe task should test data science skills and require statistical thinking and data intuition.",
  "debugging": "# Debugging Task Builder\n\nDomain Focus\nCreate tasks involving:\n- **Error diagnosis**: Stack traces, logs, error messages\n- **Root cause analysis**: Bisection, delta debugging\n- **Performance debugging**: Profiling, bottleneck identification\n- **Memory issues**: Leaks, corruption, allocation problems\n- **Concurrency bugs**: Race conditions, deadlocks, livelocks\n\nThe task should test debugging skills and require domain knowledge and analytical thinking.",
  "scientific_computing": "# Scientific Computing Task Builder\n\nDomain Focus\nCreate tasks involving:\n- **Numerical simulation**: ODEs, PDEs, Monte Carlo\n- **Signal processing**: FFT, filtering, spectral analysis\n- **Statistical analysis**: Hypothesis testing, regression, sampling\n- **Visualization**: Plotting, data exploration\n- **Domain-specific**: Physics, biology, chemistry applications\n\nThe task should test scientific computing skills and require domain knowledge and analytical thinking.",
  "data_processing": "# Data Processing Task Builder\n\nDomain Focus\nCreate tasks involving:\n- **File format handling**: CSV, JSON, XML, Parquet, binary formats\n- **Data transformation**: Cleaning, normalization, aggregation\n- **ETL pipelines**: Extract, transform, load workflows\n- **Stream processing**: Real-time data handling\n- **Data validation**: Schema enforcement, error handling\n\nThe task should test data processing skills and require domain knowledge and analytical thinking.",
  "system_administration": "# System Administration Task Builder\n\nDomain Focus\nCreate tasks involving:\n- **Process management**: Services, daemons, scheduling\n- **Network configuration**: Routing, firewall, DNS\n- **Storage management**: Filesystems, RAID, backups\n- **Monitoring**: Logging, alerting, metrics\n- **Automation**: Scripts, configuration management\n\nThe task should test system administration skills and require domain knowledge and analytical thinking."
}
````

</details>

## Request assembly and output contract

The source excerpts below are read-only documentation. Model calls return structured JSON; code in the response executes only in the remote stages shown in the walkthrough.

### recipe.py

[Source: `src/repo2rlenv/pipelines/recipes/tmax/recipe.py`](https://github.com/huggingface/Repo2RLEnv/blob/codex/owned-generation-pipelines/src/repo2rlenv/pipelines/recipes/tmax/recipe.py) · SHA-256 `db8d6df0d95ddb7cf2e1c7322e36925a63ff5da6a8c3f9827ba35e3b5d00b168`

Source hash covers the original file; trailing whitespace is omitted below.

<details class="example" markdown="1">
<summary>Read recipe.py</summary>

````python
"""TMax's retained prompts over shared initial/final-state generation stages."""

from __future__ import annotations

from importlib.resources import files

from repo2rlenv.pipelines.recipes.terminal import templates
from repo2rlenv.pipelines.recipes.tmax.sampler import template_prompt


def design(seed, **kwargs):
    resources = files(__package__)
    return templates.design(
        seed,
        template_prompt=template_prompt(seed),
        initial_prompt=resources.joinpath("initial_prompt.md").read_text(),
        final_prompt=resources.joinpath("final_prompt.md").read_text(),
        capabilities=seed["primitive_skills"],
        **kwargs,
    )


def builder_prompt() -> str:
    return templates.builder_prompt(
        files(__package__).joinpath("environment_prompt.md").read_text()
    )
````

</details>

### sampler.py

[Source: `src/repo2rlenv/pipelines/recipes/tmax/sampler.py`](https://github.com/huggingface/Repo2RLEnv/blob/codex/owned-generation-pipelines/src/repo2rlenv/pipelines/recipes/tmax/sampler.py) · SHA-256 `189db87715ee48285a774bfd2cac758a40def88af90318cb03f49108cb37f1b2`

Source hash covers the original file; trailing whitespace is omitted below.

<details class="example" markdown="1">
<summary>Read sampler.py</summary>

````python
"""Seeded sampling from TMax's retained legacy taxonomy and complexity axes."""

from __future__ import annotations

import json
import random
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SamplerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    corpus_kind: Literal["legacy"] = "legacy"
    count: int = Field(default=40, ge=1, le=1000)
    seed: int = 24
    domains: list[str] | None = None
    languages: list[str] | None = None


def sample_inputs(path: Path) -> list[dict]:
    settings = SamplerInput.model_validate_json(path.read_text())
    data = json.loads(files(__package__).joinpath("taxonomy.json").read_text())
    taxonomy = data["SKILL_TAXONOMY"]
    languages, weights = zip(*data["TASK_LANGUAGES"], strict=True)
    if settings.domains is not None and (
        not settings.domains or not set(settings.domains) <= taxonomy.keys()
    ):
        raise ValueError("Unknown or empty TMax domain selection")
    if settings.languages is not None and (
        not settings.languages or not set(settings.languages) <= set(languages)
    ):
        raise ValueError("Unknown or empty TMax language selection")
    rng = random.Random(settings.seed)
    records = []
    for draw in range(settings.count * 100):
        domain = rng.choice(list(taxonomy))
        types = taxonomy[domain]
        skill_type = rng.choice(list(types))
        all_skills = [skill for group in types.values() for skill in group]
        skills = rng.sample(all_skills, min(rng.randint(3, 5), len(all_skills)))
        complexity = rng.choice(data["TASK_COMPLEXITY"][:3])
        commands = rng.choice(data["COMMAND_COMPLEXITY"])
        scenario = rng.choice(data["DOMAIN_SCENARIOS"][domain])
        language = rng.choices(languages, weights=weights, k=1)[0]
        anchors = data["REAL_SOFTWARE_ANCHORS"].get(domain)
        anchor = rng.choice(anchors) if anchors and rng.random() < 0.35 else None
        if settings.domains is not None and domain not in settings.domains:
            continue
        if settings.languages is not None and language not in settings.languages:
            continue
        records.append(
            {
                "source": "tmax_legacy_taxonomy",
                "title": f"{domain}: {skill_type} ({draw})",
                "domain": domain,
                "skill_type": skill_type,
                "primitive_skills": skills,
                "task_complexity": complexity,
                "command_complexity": commands,
                "scenario": scenario,
                "language": language,
                "anchor": anchor,
                "corpus_kind": "legacy",
                "fixture_kind": "text_only",
                "verifier_kind": "exact_text",
                "sampler_seed": settings.seed,
                "draw": draw,
            }
        )
        if len(records) == settings.count:
            return records
    raise ValueError("Sampler selection is too restrictive for its bounded draw budget")


def template_prompt(seed: dict) -> str:
    data = json.loads(files(__package__).joinpath("taxonomy.json").read_text())
    return (
        files(__package__)
        .joinpath("template_prompt.md")
        .read_text()
        .replace("{{domain_label}}", seed["domain"].replace("_", " ").title())
        .replace("{{module}}", data["DOMAIN_MODULES"][seed["domain"]])
        .replace("{{v2_block}}", "")
    )
````

</details>
