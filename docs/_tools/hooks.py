"""Generate exact prompt references before MkDocs discovers documentation files."""

from pathlib import Path

from generate_prompt_reference import generate


def on_pre_build(config, **kwargs) -> None:
    generate(Path(config["docs_dir"]) / "pipelines/prompts")
