"""Render the prompt reference from owned sources; --check detects documentation drift."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path

from repo2rlenv.ui import console

ROOT = Path(__file__).resolve().parents[2]
RECIPES = ROOT / "src/repo2rlenv/pipelines/recipes"
OUTPUT = ROOT / "docs/pipelines/prompts"
SOURCE_URL = "https://github.com/huggingface/Repo2RLEnv/blob/codex/owned-generation-pipelines/"

# Full call-site code includes dynamic additions, user-message construction and schemas.
ASSEMBLY = {
    "swe_smith": ["swe_smith/issue.py"],
    "seta_seed2synth": ["seta_seed2synth/recipe.py"],
    "seta_evol": ["seta_evol/recipe.py"],
    "swe_gen": ["swe_gen/instruction.py"],
    "swe_flow": ["swe_flow/author.py"],
    "r2e": ["r2e/pipeline.py"],
    "tmax": ["tmax/recipe.py", "tmax/sampler.py"],
    "endless_terminals": ["endless_terminals/recipe.py", "endless_terminals/sampler.py"],
    "terminalworld": ["terminalworld/recipe.py", "terminalworld/materialize.py"],
    "cli_gym": ["cli_gym/models.py", "cli_gym/pipeline.py"],
    "dataarc": ["dataarc/recipe.py"],
    "swe_next": ["history/pipeline.py"],
    "r2e_gym": ["history/pipeline.py"],
    "scaler": ["scaler/families.py"],
}
GUIDES = {
    "swe_smith": "repo_mutate",
    "seta_seed2synth": "terminal_synth",
    "seta_evol": "task_evolve",
    "swe_gen": "pr_to_env",
    "swe_flow": "repo_reconstruct",
    "r2e": "r2e",
    "tmax": "tmax",
    "endless_terminals": "endless_terminals",
    "terminalworld": "terminalworld",
    "cli_gym": "env_repair",
    "dataarc": "dataarc",
    "swe_next": "swe_next",
    "r2e_gym": "r2e_gym",
    "scaler": "scaler",
}
TERMINAL = {"seta_seed2synth", "seta_evol", "tmax", "endless_terminals", "terminalworld", "dataarc"}


def block(path: Path, text: str | None = None, *, label: str | None = None) -> str:
    payload = path.read_text() if text is None else text
    payload = "\n".join(line.rstrip() for line in payload.splitlines())
    relative = path.relative_to(ROOT).as_posix()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    language = {".py": "python", ".json": "json"}.get(path.suffix, "text")
    # Native templates can themselves contain Markdown fences and HTML tags.
    fence = "`" * max(
        4, max((len(part) for part in payload.split() if set(part) == {"`"}), default=0) + 1
    )
    title = label or path.name
    body = f"{fence}{language}\n{payload.rstrip()}\n{fence}\n"
    return (
        f"### {title}\n\n"
        f"[Source: `{relative}`]({SOURCE_URL}{relative}) · SHA-256 `{digest}`\n\n"
        "Source hash covers the original file; trailing whitespace is omitted below.\n\n"
        f'<details class="example" markdown="1">\n<summary>Read {title}</summary>\n\n'
        + body
        + "\n</details>\n\n"
    )


def prompt_files(recipe: str) -> list[Path]:
    directory = RECIPES / recipe
    paths = list(directory.glob("*prompt.md")) + list(directory.glob("strategies/*.md"))
    for pattern in ("*demonstrations.json", "*examples.json", "strategies.json"):
        paths.extend(directory.glob(pattern))
    return sorted(paths)


def render(recipe: str, title: str) -> str:
    result = (
        f"# {title}: complete prompt reference\n\n"
        f"Read the [pipeline walkthrough](../{GUIDES[recipe]}.md) first. This reference "
        "contains the exact retained templates and the owned code that adds runtime "
        "instructions, substitutes variables, builds user messages and selects output schemas. "
        "Templates alone are not the final request.\n\n"
        "The configured `llm` is used at each model call; roles do not imply different models. "
        "Resolved requests are stored as `*.request.json` beside model receipts in the "
        "campaign, outside learner-visible bundles. See the "
        "[prompt and evidence guide](../prompt_reference.md).\n\n"
    )
    if recipe == "scaler":
        result = (
            f"# {title}: instruction construction\n\n"
            "This recipe makes **zero LLM calls**. Its generator and reference are supplied "
            "programs executed remotely. The code below shows exactly how the concrete "
            "learner instruction is assembled; it is not a system prompt sent to a model. "
            f"See the [walkthrough](../{GUIDES[recipe]}.md).\n\n"
        )
    if recipe in TERMINAL:
        result += (
            "Also read the [shared terminal additions and schemas](shared_terminal.md). "
            "They are part of the request where the walkthrough indicates the common builder.\n\n"
        )
    if recipe == "swe_flow":
        result += "The call site takes the **first two** demonstrations for each stage; full retained files are shown below.\n\n"
    if recipe != "scaler":
        result += "## Retained templates and examples\n\n"
    for path in prompt_files(recipe):
        result += block(path)
    if recipe == "tmax":
        path = RECIPES / "tmax/taxonomy.json"
        result += block(
            path,
            json.dumps(json.loads(path.read_text())["DOMAIN_MODULES"], indent=2),
            label="DOMAIN_MODULES substitutions",
        )
    if recipe == "scaler":
        result += "## Instruction assembly and instance contract\n\n"
    else:
        result += "## Request assembly and output contract\n\n"
        result += (
            "The source excerpts below are read-only documentation. Model calls return "
            "structured JSON; code in the response executes only in the remote stages "
            "shown in the walkthrough.\n\n"
        )
    for relative in ASSEMBLY[recipe]:
        result += block(RECIPES / relative)
    return result.rstrip() + "\n"


def shared() -> str:
    result = (
        "# Shared terminal prompts and output schemas\n\n"
        "SETA Seed2Synth and SETA Evol use the common builder. TMax and Endless "
        "Terminals additionally use separate template, initial-test and final-test calls. "
        "DataArc appends the common materialization contract to its own artifact prompt. "
        "TerminalWorld uses its own replay-informed materializer and shares the output "
        "schema, not the common builder request.\n\n"
    )
    path = RECIPES / "terminal/runner.py"
    source = path.read_text()
    nodes = ast.parse(source).body
    for node in nodes:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_MATERIALIZATION" for t in node.targets
        ):
            result += block(
                path, ast.literal_eval(node.value), label="Common materialization instructions"
            )
        elif isinstance(node, ast.FunctionDef) and node.name in {"feedback_for", "run_synthesis"}:
            result += block(path, ast.get_source_segment(source, node), label=node.name)
    for relative in ("terminal/templates.py", "terminal/draft.py"):
        result += block(RECIPES / relative)
    return result.rstrip() + "\n"


def quality_loop() -> str:
    root = ROOT / "src/repo2rlenv/quality/loop"
    result = (
        "# Harbor review and repair: complete prompt reference\n\n"
        "Read the [component walkthrough](../quality_loop.md) for execution, "
        "evidence and budget boundaries. These are the exact prompts, structured "
        "outputs and owned code that assembles evidence and decides when to repair.\n\n"
    )
    for relative in (
        "prompts/review.md",
        "prompts/repair.md",
        "models.py",
        "context.py",
        "runner.py",
    ):
        result += block(root / relative)
    return result.rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if committed references differ")
    args = parser.parse_args()
    catalog = {r["id"]: r for r in json.loads((RECIPES / "catalog.json").read_text())}
    expected = {
        OUTPUT / f"{recipe}.md": render(recipe, catalog[recipe]["title"]) for recipe in ASSEMBLY
    }
    expected[OUTPUT / "shared_terminal.md"] = shared()
    expected[OUTPUT / "quality_loop.md"] = quality_loop()
    changed = [
        path for path, text in expected.items() if not path.exists() or path.read_text() != text
    ]
    if args.check:
        if changed:
            console.error(
                "Prompt docs are stale. Run uv run python docs/_tools/generate_prompt_reference.py"
            )
            raise SystemExit(1)
        console.success(f"{len(expected)} prompt references match their source files")
        return
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for path in changed:
        path.write_text(expected[path])
    console.success(f"Updated {len(changed)} prompt references")


if __name__ == "__main__":
    main()
