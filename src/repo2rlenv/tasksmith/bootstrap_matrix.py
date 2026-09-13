"""Remote HF readiness matrix with explicit recipes, evidence and budget holds."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from repo2rlenv.execution.lifecycle import save_record

CPU_IMAGE = "python:3.12-slim-bookworm"
GPU_IMAGE = "pytorch/pytorch:2.11.0-cuda12.8-cudnn9-runtime"
CPU_INDEX = "https://download.pytorch.org/whl/cpu"


class RepositoryBootstrap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Literal["transformers", "accelerate", "trl", "diffusers", "peft", "tokenizers"]
    ref: str = Field(pattern=r"^[0-9a-f]{40}$")
    # Explicit repair commands are configuration, never silently applied patches.
    extra_install: list[str] = Field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://github.com/huggingface/{self.name}"


def dockerfile(spec: RepositoryBootstrap, resource: str, *, clone: bool = False) -> str:
    if resource not in {"cpu", "gpu"}:
        raise ValueError("Bootstrap resource must be cpu or gpu")
    base = CPU_IMAGE if resource == "cpu" else GPU_IMAGE
    lines = [
        f"FROM {base}",
        "USER root",
        "ENV DEBIAN_FRONTEND=noninteractive",
        "RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates build-essential pkg-config libssl-dev && rm -rf /var/lib/apt/lists/*",
    ]
    if resource == "gpu":
        # The official 2.11 runtime uses externally managed system Python.
        # Keep its preinstalled CUDA PyTorch visible in an isolated writable venv.
        lines.extend(
            [
                "RUN apt-get update && apt-get install -y --no-install-recommends python3-venv && rm -rf /var/lib/apt/lists/*",
                "RUN python -m venv --system-site-packages /opt/tasksmith-venv",
                "ENV PATH=/opt/tasksmith-venv/bin:$PATH",
                "RUN python -c 'import torch; assert torch.version.cuda is not None'",
            ]
        )
    lines.append(
        "RUN python -m pip install --no-cache-dir 'setuptools>=77.0.3' wheel 'pytest>=8,<9' parameterized"
    )
    if resource == "cpu":
        lines.append(
            f"RUN python -m pip install --no-cache-dir torch==2.11.0 --index-url {CPU_INDEX}"
        )
    if spec.name == "tokenizers":
        lines.extend(
            [
                "RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal",
                "ENV PATH=/root/.cargo/bin:$PATH",
            ]
        )
    lines.append("WORKDIR /workspace")
    if clone:
        lines.append(
            f'RUN git init -q . && git fetch --depth=1 {spec.url} {spec.ref} && git checkout --detach FETCH_HEAD && test "$(git rev-parse HEAD)" = {spec.ref}'
        )
    else:
        lines.append("COPY . /workspace")
    package = "./bindings/python" if spec.name == "tokenizers" else "."
    lines.append(f"RUN python -m pip install --no-cache-dir -e {package}")
    for command in spec.extra_install:
        if not command.strip() or "\n" in command or "\r" in command:
            raise ValueError("Repair install commands must be nonempty single lines")
        lines.append("RUN " + command)
    lines.extend(
        [
            "RUN python -m pip check && python -m pip freeze > /opt/bootstrap-freeze.txt",
            "RUN rm -rf /workspace/.git /root/.cache/pip",
            "ENV HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 PYTHONDONTWRITEBYTECODE=1 REPO2RLENV_REMOTE_WORKER=1",
        ]
    )
    return "\n".join(lines) + "\n"


def cpu_build(spec: RepositoryBootstrap, output: Path) -> dict:
    """Runs inside the existing Docker worker, never on the controller."""
    import os
    import subprocess
    from dataclasses import asdict

    from repo2rlenv.bootstrap import ensure_bootstrap
    from repo2rlenv.spec.input import AuthSpec, BootstrapSpec, LLMSpec, RepoSpec

    if os.environ.get("REPO2RLENV_REMOTE_WORKER") != "1":
        raise RuntimeError("Bootstrap matrix target builds are remote only")
    output.mkdir(parents=True, exist_ok=True)
    recipe = output / "Dockerfile"
    recipe.write_text(dockerfile(spec, "cpu"))
    boot = ensure_bootstrap(
        RepoSpec(url=spec.url, ref=spec.ref, access="public"),
        BootstrapSpec(
            user_dockerfile=recipe, cache_dir=Path("/work/hf-bootstrap-cache"), max_seconds=1800
        ),
        LLMSpec(provider="none", model="explicit-dockerfile"),
        AuthSpec(use_gh_cli=False),
    )
    save_record(output / "bootstrap.json", asdict(boot))
    smoke = Path(__file__).with_name("bootstrap_smoke.py")
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--cpus",
            "4",
            "--memory",
            "8g",
            "-v",
            f"{smoke}:/opt/bootstrap-smoke.py:ro",
            boot.image_digest,
            "python",
            "/opt/bootstrap-smoke.py",
            spec.name,
            "cpu",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    (output / "smoke.stdout").write_text(result.stdout)
    (output / "smoke.stderr").write_text(result.stderr)
    if result.returncode:
        raise ValueError(f"{spec.name} CPU behavior failed; see smoke logs")
    freeze = subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            boot.image_digest,
            "cat",
            "/opt/bootstrap-freeze.txt",
        ],
        text=True,
        timeout=30,
    )
    (output / "pip-freeze.txt").write_text(freeze)
    # Exclude the editable target package. These hints cannot expose its source
    # or force consumers to depend on a private repository-containing image.
    dependencies = [
        line for line in freeze.splitlines() if "==" in line and not line.startswith("#")
    ]
    from repo2rlenv.tasksmith.worker import build_dependency_image

    dependencies += ["--extra-index-url", CPU_INDEX]
    dependency_cache = build_dependency_image(CPU_IMAGE, dependencies, output)
    result = {
        "status": "ready",
        "source": spec.model_dump(),
        "image": boot.image_digest,
        "smoke": json.loads(result.stdout),
        "dockerfile_sha256": hashlib.sha256(recipe.read_bytes()).hexdigest(),
        "hint": {
            "ref": spec.ref,
            "base_image": CPU_IMAGE,
            "dependencies": dependencies,
            "scope": "repository smoke only; recheck this PR's dependencies and tests",
        },
        "dependency_cache": dependency_cache,
    }
    save_record(output / "result.json", result)
    return result


def remote_main(config: Path, output: Path) -> None:
    """Supervisor entrypoint; preserve each failed repository independently."""
    import traceback

    rows = []
    for data in json.loads(config.read_text()):
        spec = RepositoryBootstrap.model_validate(data)
        destination = output / spec.name
        try:
            row = cpu_build(spec, destination)
        except Exception as exc:
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "error.txt").write_text(traceback.format_exc())
            row = {"status": "failed", "source": data, "error": str(exc)}
            save_record(destination / "result.json", row)
        rows.append(row)
        save_record(output / "report.json", {"repositories": rows})
        print(spec.name, row["status"], flush=True)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    remote_main(args.config, args.output)
