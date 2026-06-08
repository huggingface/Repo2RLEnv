# Coding Environments 101: Why Harbor Exists

*I used Claude Code to help me put this together. I've been working with Harbor pretty extensively across a bunch of projects, and figured it was time to write down why I think it's the right abstraction.*

Coding as an RL task is having a moment. And if you've actually tried to do RL on a coding task, you already know the model is the easy part. It's everything *around* the model that eats your week.

You need a real environment. Right toolchain, right system libs, deps pinned, GPU drivers if you need them, a way to reset state between rollouts. You need to pick a coding harness. Claude Code? Aider? OpenHands? Your own scaffold with bash and an editor? You need initialization points. What commit are you starting from, what's the starting prompt, what files does the agent see first? And you need a reward you can actually trust. Run the tests, score the diff, ask a judge, all of the above.

Every one of those decisions is its own rabbit hole. And most of them get reinvented from scratch every time someone publishes a new coding benchmark or RL recipe. This blog is about why that's not sustainable, and why the shape of the answer is starting to look like a framework called **Harbor**.

## RL on code is winning because the reward is verifiable

The pattern over the last 18 months in code LLMs is hard to miss. Everyone moved to the same idea: stop pretending pretraining is enough, train against signals that compile and run. Every frontier lab is now post-training with some flavor of reinforcement learning with verifiable rewards (RLVR), and code is the cleanest domain to do it in. `pytest` doesn't lie.

A non-exhaustive tour of who's doing what:

- **Meta's [SWE-RL](https://arxiv.org/abs/2502.18449)** trained Llama3-SWE-RL-70B with a rule-based reward (diff similarity against ground-truth PR patches) on millions of merged pull requests. It hit 41% on SWE-bench Verified, at the time the best for any sub-100B model, and generalized to math and language reasoning even though training was 100% code. ([code](https://github.com/facebookresearch/swe-rl))
- **OpenAI's Codex (`codex-1`)** is a version of o3 [trained with RL on real-world coding tasks across many environments](https://openai.com/index/o3-o4-mini-codex-system-card-addendum/), "iteratively running tests until passing." o3 itself jumped from 48.9% to 71.7% on SWE-bench Verified over o1 from this kind of training.
- **DeepSeek-R1** ([Nature, 2025](https://www.nature.com/articles/s41586-025-09422-z)) showed you can get reasoning from *pure* RL with verifiable rewards, no SFT warm-up. They used GRPO and explicitly used compiler feedback on LeetCode problems as the signal.
- **Moonshot's Kimi K2** ([tech report](https://arxiv.org/abs/2507.20534)) introduced a "Gym-like extensible framework" for scaling RL across diverse coding scenarios. 65.8% on SWE-bench Verified with just a bash/editor harness.
- **Qwen3-Coder-Next** ([report](https://arxiv.org/abs/2603.00729)) is explicit about it: "large-scale synthesis of verifiable coding tasks paired with executable environments" is the central training advance, not the model architecture.
- **Prime Intellect's [INTELLECT-3](https://www.primeintellect.ai/blog/intellect-3)** is a 100B+ MoE trained with large-scale RL across thousands of community-contributed environments.
- **Anthropic's Claude 4.x** family is famously good at code. The [public training detail](https://www.anthropic.com/news/claude-sonnet-4-5) is thinner, but Anthropic is reportedly scaling RL environment spend [3 to 5x into 2026](https://www.wing.vc/content/rl-environments-for-agentic-ai-who-will-win-the-training-verification-layer-by-2030).

The thesis is now consensus: coding is uniquely well suited to RL because the reward is *verifiable*. You can run the code and check. No reward model, no human-labeled preferences, no drift. As [Sebastian Raschka summarized it](https://magazine.sebastianraschka.com/p/the-state-of-llm-reasoning-model-training), this is why every reasoning-model paper of the last year reads like the same paper.

## The new bottleneck isn't the model. It's the environment.

Here's the catch nobody warns you about. The moment you commit to RL with verifiable rewards, your training run is only as good as the environments you can spin up. And environments are *hard*.

A recent [HF blog walking through Prime Intellect's Environments Hub](https://huggingface.co/blog/anakin87/environments-hub) puts it bluntly:

> "The current ecosystem for environments is fragmented. Implementations are often tightly coupled with a specific training stack, making them difficult to adapt, reuse, or share... Without a robust open alternative, open models could lag behind, leaving users reliant on closed models whose capabilities are shaped by inaccessible tools."

Prime Intellect, who run [the Environments Hub](https://www.primeintellect.ai/blog/environments) with 1,000+ open-source RL environments, frame it the same way. The path to scaling RL is not bigger models but more, cheaper, more diverse environments.

[Wing Venture's analysis](https://www.wing.vc/content/rl-environments-for-agentic-ai-who-will-win-the-training-verification-layer-by-2030) calls it directly: *"verification, not models, is the true bottleneck to automation."* MiniMax's Forge framework runs RL training [across 100,000+ distinct agent scaffolds and environments](https://huggingface.co/blog/async-rl-training-landscape) with daily throughput in the millions of samples. That's not a model problem. That's an infrastructure problem.

If you want a better understanding of what RL environments are in general, refer to [this article called Ultimate Guide to RL Environments](https://huggingface.co/spaces/AdithyaSK/rl-environments-guide).

And the shape of the problem is always the same. Every coding environment needs three things:

1. **A frozen state or snapshot.** A specific commit, a codebase, a dataset, or any fixed place the task can start from, with dependencies pinned.
2. **A way to run it.** A Docker image with the right Python/Node/Go toolchain, system libs, GPU drivers if needed.
3. **A way to score the agent's output.** Tests, a diff comparison, a judge model, a custom verifier.

Every benchmark team in the world has rebuilt this stack from scratch. SWE-bench has its harness. R2E has its own. Magicoder has its own. The result is what HF called "fragmentation": agents can't move between benchmarks, datasets can't compose, and every lab burns months on plumbing they'd rather not own.

## Enter Harbor

[Harbor](https://github.com/harbor-framework/harbor) is a coding-environment framework from the team behind [Terminal-Bench](https://github.com/harbor-framework/terminal-bench). It was [announced alongside Terminal-Bench 2.0](https://www.tbench.ai/news/announcement-2-0) by Mike Merrill and Alex Shaw, and their framing of why they built it is the cleanest articulation of the problem I've seen:

> "Evaluating in containers proved slow and difficult to scale... Limited capabilities for improving agents through training and optimization methods... Fragmentation across agent frameworks and benchmarks required specialized solutions for each deployment."

Harbor is the harness that came out of Terminal-Bench's own pain. The authors' own [task-difference doc](https://github.com/harbor-framework/harbor/blob/main/docs/content/docs/tasks/task-difference.mdx) calls out concrete fixes: instructions live in markdown instead of nested YAML, the task config is typed (Pydantic), the environment definition is mandatory and pluggable, solutions can be multi-file, and (critically) **rewards are produced by the task, not parsed by the harness**. That last one matters. It's how Harbor opens the door to non-binary rewards, judge scores, and multi-criteria evaluation without changing the framework itself.

The answer Harbor lands on is a minimal, opinion-having standard. A task is a directory, a directory has four files, and any agent that can run in a container can run any task. The interface is small enough to learn in an afternoon and structured enough that you can build serious training infrastructure on top of it.

That's the part I want to spend the rest of this post on, because the abstraction Harbor picked is, in my opinion, exactly the right one. It gives you enough structure to interop, without taking away the freedom to do weird things when you need to.

## The task: four files, one contract

A Harbor task lives on disk like this:

```
hello-world/
├── instruction.md           # What the agent should do, in English
├── task.toml                # Typed config: timeouts, resources, metadata, MCP servers
├── environment/
│   └── Dockerfile           # (or docker-compose.yaml for multi-container)
├── solution/
│   └── solve.sh             # Optional oracle solution
└── tests/
    └── test.sh              # Verifier: writes reward to /logs/verifier/
```

Each file answers exactly one question. That's why it's a good abstraction.

- `environment/Dockerfile` answers *how do I run this?*
- `instruction.md` answers *what should the agent do?*
- `tests/test.sh` answers *how do I know it succeeded?*
- `task.toml` answers *who built this, for what, with what limits?* It's a real typed schema, not a free-form config, with sections for CPU/memory/GPU, internet allowlist, MCP servers exposed to the agent, container healthchecks, and verifier API keys.

A trivial verifier is just a bash script that writes a number:

```bash
#!/bin/bash
OUTPUT=$(python /workspace/hello.py 2>&1)
if [ "$OUTPUT" = "Hello, World!" ]; then
    echo "1" > /logs/verifier/reward.txt
else
    echo "0" > /logs/verifier/reward.txt
fi
```

For multi-metric rewards you write `reward.json` instead (e.g. `{"correctness": 0.75, "structure": 1.0}`) and Harbor handles the aggregation. Anywhere along the way, the reward is just numbers in a known location. That uniformity is the entire trick.

For tasks where one shot isn't enough, Harbor supports [multi-step tasks](https://github.com/harbor-framework/harbor/blob/main/docs/content/docs/tasks/multi-step.mdx). Replace `instruction.md` and `tests/` with `steps/<step-name>/` directories and declare them in `task.toml`. Per-step rewards aggregate via `MEAN` or `FINAL` strategies, with optional `min_reward` thresholds that early-stop the trial if a step bombs.

## Bring your own agent harness

Harbor ships with **26 built-in agent adapters** out of the box: Claude Code, Codex CLI, OpenHands, Gemini CLI, Aider, Goose, Cursor CLI, Cline CLI, Copilot CLI, OpenCode, Qwen Coder, Kimi CLI, Mini-SWE-Agent, SWE-Agent, Trae Agent (ByteDance), Rovodev (Atlassian), NeMo Agent (NVIDIA), plus Harbor's own [Terminus-2](https://github.com/harbor-framework/harbor/blob/main/docs/content/docs/agents/terminus-2.mdx) reference agent and an `oracle` adapter that just runs `solution/solve.sh` for sanity checks. ([source list](https://github.com/harbor-framework/harbor/blob/main/src/harbor/models/agent/name.py))

But the most underrated thing Harbor did is decouple the **task** from the **agent harness**. The contract for a custom harness is tiny. Subclass `BaseAgent`, implement `setup()` and `run()`, run with `--agent-import-path your.module:YourAgent`. That's the whole API. Anywhere you can install a binary in a container, you have a Harbor harness. Your hand-rolled scaffold with bash and a file editor, your custom multi-agent rig, somebody's research prototype, all the same.

The flip side is just as important. Any task you write works with **any** agent. Train your model with one harness, eval it with another, ship a third. The task doesn't know or care. This is the USB-C of agent harnesses.

## Rewards beyond pass/fail

The reward contract (numbers in `/logs/verifier/reward.{txt,json}`) is thin on purpose. Anything you can compute inside the container, you can write as a reward. Harbor leans into this with first-class support for:

- **Test-execution rewards.** The canonical case: run pytest/cargo/jest, write 1.0 or 0.0.
- **Diff-similarity.** Write the sequence similarity between agent diff and oracle, give partial credit for partial fixes (the SWE-RL recipe).
- **[LLM-as-a-judge](https://www.harborframework.com/docs/tutorials/llm-as-a-judge).** The verifier shells out to a judge model, gets back a 0 to 1 score. API keys flow through a clean `[verifier.env]` block in `task.toml`, so the same task can be scored by Claude, GPT-5, whatever. The repo ships a [worked example](https://github.com/harbor-framework/harbor/tree/main/examples/tasks/llm-judge-example): a "write a funny poem" task graded by Claude with a JSON-schema rubric.
- **Step-level rewards.** Multi-step tasks emit per-step `verifier_result`s, aggregated by `MEAN` or `FINAL`.
- **Reward Kit.** Harbor also ships [`harbor-rewardkit`](https://github.com/harbor-framework/harbor/tree/main/examples/tasks/reward-kit-example), a separate package with 20+ built-in primitives (`file_exists`, `command_succeeds`, ...) plus TOML-defined LLM judges *and* **agent-as-judge** rubrics. Agent judges run in overlayfs-isolated copies of the workspace so they can explore and run commands without polluting each other. Aggregation supports weighted mean, all-pass, any-pass, threshold. One-liner `test.sh`:
  ```bash
  uvx --with harbor-rewardkit@0.1 rewardkit /tests
  ```

The point isn't that Harbor has *every* reward type built in. The point is that the reward contract is so thin you can implement any reward without touching the framework, and the framework provides batteries-included tooling for the common cases. That's the right level of abstraction.

## The uniform output spec: ATIF

Here's the under-talked-about thing Harbor did. It makes every agent's output look the same. The format has a name: **ATIF**, the [Agent Trajectory Interchange Format](https://github.com/harbor-framework/harbor/blob/main/rfcs/0001-trajectory-format.md), maintained as RFC-0001 (currently v1.7).

Whether you ran Claude Code, OpenHands, Codex, Mini-SWE-Agent, Gemini CLI, or your own homegrown scaffold, Harbor's adapters convert the trace to a uniform JSON shape. Per-step records with `source`, `model_name`, `message`, `reasoning_content`, `tool_calls`, `observation`, and a `metrics` block with token counts, cached tokens, and cost in USD. Root-level metadata covers session ID, agent identity, and final aggregated metrics.

Why does this matter? Because the moment your traces are uniform, **they're training data**. And the ATIF authors made this explicit:

- v1.3 added `completion_token_ids` "to enable RL training without retokenization drift"
- v1.4 added `prompt_token_ids` for the same reason
- v1.5 added `tool_definitions` for SFT pipelines

You can read this version history backwards as Harbor figuring out, in public, what RL pipelines actually need from a trace format. The result is a JSON spec that's first-class training input. Harbor has dedicated docs for both [SFT](https://github.com/harbor-framework/harbor/blob/main/docs/content/docs/training-workflows/sft.mdx) and [RL](https://github.com/harbor-framework/harbor/blob/main/docs/content/docs/training-workflows/rl.mdx) workflows, and ships a [SkyRL integration](https://github.com/NovaSky-AI/SkyRL) where the rollout interface returns `Rollout(reward=..., token_ids=..., mask_ids=...)` straight out of a Harbor trial.

This is what closes the loop. Same task spec → same execution sandbox → same reward signal → same trace format → trainable. You can run thousands of rollouts across five different agent harnesses in parallel, dump them into one bucket, and feed them straight into post-training. No reshaping. No glue code. Nobody else got all four right.

([Hugging Face's TRL recently shipped OpenEnv integration](https://huggingface.co/docs/trl/main/openenv) on the back of this same standardization push. The dust is settling around uniform environment interfaces, and Harbor was early.)

## Getting started

The smallest end-to-end loop:

```bash
# Install
uv tool install harbor

# Run a real benchmark with Claude Code, 4 parallel containers
export ANTHROPIC_API_KEY=<YOUR-KEY>
harbor run \
  --dataset terminal-bench@2.0 \
  --agent claude-code \
  --model anthropic/claude-opus-4-1 \
  --n-concurrent 4
```

Or run a single task locally with the oracle to confirm your verifier is sane:

```bash
uv run harbor run --agent oracle --path ./tasks/hello-world
# Mean: 1.000 ✓
```

Want it on Modal or Daytona instead? Add `--env modal` or `--env daytona` and you have a thousand parallel containers in the cloud. Harbor supports **10+ sandbox backends** behind one flag: Local Docker, Daytona, Modal, E2B, Runloop, Apple Container, GKE, Tensorlake, Islo, Singularity. Same task spec. Same reward signal. Same trace output.

Inspect everything afterwards with `harbor view`, a local web UI that renders trajectories, per-criterion reward breakdowns, and collected artifacts. And if you want to skip writing tasks from scratch, Harbor already ships [50+ benchmark adapters](https://github.com/harbor-framework/harbor/tree/main/adapters): SWE-Bench family, Aider Polyglot, LiveCodeBench, GAIA, BFCL, MedAgentBench, ML-Dev-Bench, and more. Every major coding/agent benchmark, normalized to the same `harbor run` flow.

That's the whole pitch.

## Why this matters now

The next year of frontier coding-model work is going to be dominated by who can stand up the most environments, the fastest, with the highest verification fidelity. Anthropic is signing environment contracts. Prime Intellect is running a community hub of 1,000+. Hugging Face is shipping OpenEnv. The bottleneck is shared infrastructure, and we don't have time to keep rebuilding it.

Harbor is the simplest version of that shared infrastructure that doesn't lose. Four files, any agent, 10+ sandboxes, uniform trainable traces. If you've ever rebuilt the same eval scaffolding twice, that's the case for picking it up.

---

**Sources:**

- Lab RL work: [SWE-RL](https://arxiv.org/abs/2502.18449) · [Codex/o3](https://openai.com/index/o3-o4-mini-codex-system-card-addendum/) · [DeepSeek-R1](https://www.nature.com/articles/s41586-025-09422-z) · [Kimi K2](https://arxiv.org/abs/2507.20534) · [Qwen3-Coder-Next](https://arxiv.org/abs/2603.00729) · [INTELLECT-3](https://www.primeintellect.ai/blog/intellect-3)
- Environments discourse: [HF Environments Hub blog](https://huggingface.co/blog/anakin87/environments-hub) · [Prime Intellect Environments](https://www.primeintellect.ai/blog/environments) · [Wing VC analysis](https://www.wing.vc/content/rl-environments-for-agentic-ai-who-will-win-the-training-verification-layer-by-2030) · [Sebastian Raschka, State of RL](https://magazine.sebastianraschka.com/p/the-state-of-llm-reasoning-model-training) · [The ultimate guide to RL environments](https://huggingface.co/spaces/AdithyaSK/rl-environments-guide)
- Harbor: [GitHub](https://github.com/harbor-framework/harbor) · [docs](https://www.harborframework.com/) · [Terminal-Bench 2.0 announcement](https://www.tbench.ai/news/announcement-2-0) · [ATIF (RFC-0001)](https://github.com/harbor-framework/harbor/blob/main/rfcs/0001-trajectory-format.md) · [LLM-as-judge tutorial](https://www.harborframework.com/docs/tutorials/llm-as-a-judge) · [Reward Kit example](https://github.com/harbor-framework/harbor/tree/main/examples/tasks/reward-kit-example) · [Multi-step tasks](https://github.com/harbor-framework/harbor/blob/main/docs/content/docs/tasks/multi-step.mdx) · [Tessl intro](https://tessl.io/blog/how-to-evaluate-ai-agents-an-introduction-to-harbor/)
- Training-stack standardization: [TRL OpenEnv integration](https://huggingface.co/docs/trl/main/openenv) · [HF, Keep the Tokens Flowing](https://huggingface.co/blog/async-rl-training-landscape)

---

*PS: if you spot anything factually off, please let me know. Feedback and suggestions are always welcome. Thanks!*
