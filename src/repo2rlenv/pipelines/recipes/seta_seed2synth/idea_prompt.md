# Seed-to-Idea Agent: Standard Workflow

## Your Position in the Pipeline

- **Stage 1 (Your Role)**: Read seed data → Analyze core capabilities → Evolve into a realistic terminal task → Write `draft_spec.md`
- **Stage 2 (Datapoint Builder Agent)**: Takes your `draft_spec.md` → Builds the full Harbor task (`task.toml`, `instruction.md`, `environment/Dockerfile`, `solution/solve.sh`, `tests/`) → Validates and finalizes

The datapoints you help create are used in RL training for an AI agent that:
- Operates in Linux Docker containers via the **Harbor** framework
- Completes terminal-based tasks autonomously (up to 50 turns)
- Uses bash, file operations, and search tools — no user interaction, no browser
- Must plan, explore, execute, and verify solutions independently

---

## Workflow

### Step 2: Design the Task

In a single pass of reasoning (no separate tool calls needed), work through all of the following:

**DAG reasoning** — Model the solution path as a Directed Acyclic Graph. For each node identify the terminal/system capability exercised, its prerequisites, and the observable postcondition. This becomes your **Reasoning Steps Required** list.

**Analysis** — Identify the command-line tools/services/languages involved, relevant filesystem/process/network patterns, distinct sub-problems, failure modes, and how the agent verifies correctness.

**Tech stack** — Choose a base Docker image, required packages, language versions, and any external services.

**Evolution** — Transform the seed into a realistic multi-step terminal task (see principles below).

---

### Step 3: Web Research (conditional — skip if not needed)

**Only search if** the technology is niche, poorly documented, or has version-specific behavior you are not certain of.
**Skip entirely** for common Linux tools (systemd, cron, iptables, nginx, ssh, bash scripting, standard apt packages, Python stdlib, etc.) — your training data covers these well.

If you do search:
- **Cap at 1 WebSearch + 1 WebFetch total**
- Target official docs or man pages for the most specific unknown (e.g. exact config syntax, obscure flag behavior)
- Include the URL and key excerpt in `## External Resources`

Do NOT search just to "confirm" things you already know.

---

### Step 4: Evolve into a Terminal Task

Transform the seed into a realistic, multi-step terminal task. Principles:

1. **Preserve the core** — the evolved task must exercise the same fundamental capability as the seed
2. **Create a real scenario** — do not just ask the agent to run a known command; wrap it in a believable environment where the agent must explore, diagnose, and act
3. **Require multi-step reasoning** — the task must not be solvable with one or two commands
4. **Add realistic constraints** — broken environments, pre-existing configs, permissions, edge-case data
5. **Ensure deterministic, testable outcomes** — results can be verified by Python pytest

**Layered complexity patterns:**
- Conflicting configs that must be reconciled
- Hidden issues that only surface after initial setup
- Multiple interacting services or files
- Edge cases that break naive approaches

**Instruction hygiene** (critical):
The eventual `instruction.md` shown to the agent must contain only:
- What the agent needs to do — the goal or observable problem (for a broken system: what fails; for a build/config task: what needs to exist or work)
- Entry points (commands, scripts, paths, or services the agent can start from)
- Acceptance criteria (what success looks like from the outside)
- Environment constraints the agent can observe

It must NOT contain: the exact commands to run, config values to set, which files to modify, or anything else that reveals what `solve.sh` does or what `test.sh` asserts.

---

### Step 5: Write `draft_spec.md`

Write the file to the output path shown at the top of your instructions (e.g. `<output_path>/draft_spec.md`). **Always use the full absolute path** when calling the Write tool. **Do not re-read the file after writing** — the Write tool confirms success.

---

## Required Output Format (`draft_spec.md`)

```markdown
## Task
[One sentence: what the agent must accomplish]

## Agent-Visible Task Brief
**Goal**: [What the agent must accomplish — the observable problem or task (e.g. "X is broken", "build Y", "process Z") — no implementation details]
**Entry Points**: [Commands, scripts, paths, or services the agent can use to begin]
**Acceptance Criteria**: [Concrete, observable outcomes that define success]
**Environment Constraints**: [Runtime, network, file, or service constraints the agent can observe]
**Visible Paths**: [File paths and directories the agent is expected to know up front]

## Builder-Only Notes
**Hidden Details**: [Exact steps, values, files, or configs the oracle uses — never copy these into instruction.md]
**Dependency Chain**: [Why step A must happen before step B — ensures the oracle can script steps in order]

## Instructions
[Builder guidance: what to construct, what to plant, what to make broken — without prescribing the agent's solution path]

## Source Context
[Which files from the seed folder were read and how they shaped the task design]

## Environment Setup
[Docker base image, apt/pip packages, pre-seeded files and their content, services to run]

## Reasoning Steps Required
[Numbered list of distinct steps the agent must take. Count them before assigning difficulty.]
1. ...
2. ...
...

## Testing
[Describe 5–10 specific unit tests by intent, not code. For each: what to verify and how.]

## Difficulty
[easy | medium | hard — must match the step count above; see calibration table]

## Core Skills Tested
[Bullet list of technical and cognitive skills]

## Key Technologies
[Main tools, languages, services]

## External Resources
[URLs and key excerpts from web research that informed the task design]
- URL: <url> — Key excerpt: <relevant content>
```

---

## Difficulty Calibration

Choose difficulty **after** counting Reasoning Steps Required:

| Difficulty | Reasoning Steps | When to use |
|---|---|---|
| `easy` | 0–10 steps | Single-tool or single-file task with a clear, well-scoped goal |
| `medium` | 10–30 steps | Focused single-service or multi-step problem with clear success criteria |
| `hard` | 30+ steps | Multiple interacting components, hidden failure modes, or non-obvious diagnosis chain |

Rules:
- Prefer `medium` as the default — the training distribution benefits most from well-scoped medium tasks
- `easy` is acceptable when the seed naturally fits under 10 steps; do not inflate it artificially
- `very hard` is not a valid Harbor value — use `hard` for the most complex tasks
- Complexity must come from the **environment**, not from cramming in unrelated requirements

---

## Test Count and Quality

**Always write 5–10 unit tests**, regardless of difficulty.

### Test Type Mix
- **Core functionality** (2–3): Verify the main requirements work correctly
- **Edge case / error handling** (1–2): Unusual inputs, missing files, invalid data, failure scenarios
- **Integration / correctness** (1–2): Components work together; outputs contain correct values, not just correct format
- **Validation** (1): Deeper correctness — actual computed values, not just structure

### Test Quality Checklist

1. **No vacuous passes** — every assertion must fail when the fix is absent. Guard clauses like `if len(rows) > 0: assert ...` silently pass on empty results — use `assert len(rows) > 0` as a separate check first.
2. **No code-inspection tests** — never verify fixes by string-matching source code. Always test runtime behavior.
3. **Test independence** — each test creates its own required state. Do not rely on execution order.
4. **Service verification** — if the task involves running services, at least one test must start the service and hit a live endpoint or run the pipeline end-to-end.
5. **Strict assertions** — avoid loose checks. Each assertion has exactly one expected outcome.
6. **No answer leakage** — test helpers must not hardcode the correct answer in a way that reveals the fix.
7. **Reference data correctness** — if tests compare against expected output, verify the reference was generated with the exact same parameters.

### Examples
- ✅ `assert report["broken_count"] == 3 and report["clean_count"] == 17`
- ✅ `assert response.status_code == 200 and response.json()["version"] == "3.2.1"`
- ❌ `assert os.path.exists(output_file)` (too shallow — verify content, not existence)
- ❌ `assert isinstance(result, dict)` (only checks type, not correctness)

---

## Platform Requirement

**All tasks must run on Linux/Ubuntu.** This is non-negotiable.

If the seed data describes a problem on macOS, Windows, or another OS:
1. Identify the equivalent Linux/Ubuntu tools, paths, and behaviors
2. Adapt the scenario to Linux before designing the task (e.g. `brew` → `apt`, `/Users/` → `/home/`, macOS `launchd` → `systemd`, Windows Registry → config files)
3. Do not create a task that requires macOS- or Windows-specific syscalls, filesystem semantics, or tooling with no Linux equivalent

When in doubt, the task environment is always: **Ubuntu 24.04**, bash shell, systemd init.

---

## Constraints

- Docker build must complete in under 5 minutes — target image size ~500 MB, hard limit 1 GB; no heavy base images (no `nvidia/cuda`, no `pytorch/pytorch`)
- Use common base images: `ubuntu:24.04`, `python:3.13`, `node:20`, etc.
- Pre-install `uv` and `tmux` in the Dockerfile — `uv` avoids network at test time; `tmux` is required by the agent's shell tool
- **No GPU tasks** — the container has CPU only; do not design tasks that require a GPU or CUDA
- **No model training** — tasks must not require training or fine-tuning ML models (inference on tiny pre-existing models is acceptable only if the model fits in the image and loads in seconds)
- **No heavy compute** — tasks must complete well within the agent timeout; avoid anything that would take tens of minutes of CPU (e.g. compiling a large codebase from scratch, brute-force search over large datasets)
- Container resources: 1 CPU, 2 GB RAM, 10 GB storage — design tasks that fit comfortably within these limits
- Tasks must be completable within 50 agent turns
- Terminal-based only — no GUI applications
- Deterministic, reproducible outcomes
- Agent has network access for package installs but NO browser or web search
- Tests run inside the container after the agent completes
- Tests are always Python (pytest), regardless of task implementation language

---

## Quality Guidelines

### What makes a good task
- Teaches a specific skill the RL agent needs to practice
- Mirrors a real developer or sysadmin scenario
- Has unambiguous pass/fail conditions
- Allows multiple valid approaches while having deterministic test outcomes

### Red flags — avoid these
- Root-cause leakage: instruction tells the agent which module is broken, which config value is correct, or which exact fix to apply
- Over-specified solutions: forces one exact implementation path
- Single-component tasks with only 1–2 meaningful tests — the task is probably too simple
- Linear tasks where each step is obvious from the previous one
- Shallow testing: checking file existence or JSON format without verifying actual values
