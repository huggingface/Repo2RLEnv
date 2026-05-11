"""Data types for the bootstrap module."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class LanguageHint(StrEnum):
    PYTHON = "python"
    NODE = "node"
    GO = "go"
    RUST = "rust"
    JAVA = "java"
    C_CPP = "c_cpp"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class BootstrapResult:
    """What `ensure_bootstrap()` returns.

    The image_digest is the load-bearing field — once set, the bootstrap is
    fully reproducible: any sandbox can pull `image_digest` and run tasks
    against it without re-running the agent.
    """

    image_digest: str                   # ghcr.io/.../bootstrap@sha256:... or local-only digest
    image_tag: str                      # human-readable tag we committed to
    language: LanguageHint
    repo: str                           # "owner/name"
    ref: str                            # commit SHA the bootstrap built from
    rebuild_cmds: list[str]             # commands to re-apply build after a patch
    test_cmds: list[str]                # commands to run the test suite
    smoke_passed: bool                  # did the smoke test pass at bootstrap time
    iterations: int                     # how many agent turns it took
    build_time_sec: float
    llm_provider: str                   # "anthropic/claude-sonnet-4-6"
    llm_cost_estimate_usd: float = 0.0  # rough running total
    dockerfile_reconstruction: str = "" # generated from the agent's commands, for reproducibility
    transcript_path: Path | None = None # full ReAct transcript for debugging
    pushed_to_registry: bool = False
    extra: dict = field(default_factory=dict)
