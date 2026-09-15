"""Native GPU bootstrap and construction; local work only materializes artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath

import tomli_w

from repo2rlenv.campaigns.budget import BudgetExceeded
from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.pipelines.recipes.repository.export import (
    export_repository_task,
    private_asset,
    repository_build,
)
from repo2rlenv.quality.loop.artifacts import refresh_identity, task_identity
from repo2rlenv.quality.python_evidence import test_excerpts
from repo2rlenv.quality.test_results import execution_contrast, parse_junit
from repo2rlenv.tasksmith.models import Design, Profile
from repo2rlenv.tasksmith.readiness import validate_readiness_paths


async def check_context(
    context: Path, profile: Profile, output: Path, budget, gpus: int, *, tests: bool = True
):
    """Build on Modal, assert real device allocation and run the pinned test selection."""
    from harbor.models.task.config import EnvironmentConfig, NetworkPolicy
    from harbor.models.trial.paths import TrialPaths

    from repo2rlenv.execution.harbor_modal import (
        ACCOUNTING,
        MeteredModalEnvironment,
        NativeAccounting,
    )

    output.mkdir(parents=True, exist_ok=False)
    accounting = NativeAccounting(budget, output / "allocations")
    token = ACCOUNTING.set(accounting)
    environment = None
    try:
        config = EnvironmentConfig(
            cpus=4, memory_mb=16384, gpus=gpus, gpu_types=["L4"], network_mode="no-network"
        )
        name = budget.prefix + "-" + hashlib.sha256(str(output).encode()).hexdigest()[:10]
        environment = MeteredModalEnvironment(
            environment_dir=context,
            environment_name="tasksmith-gpu-check",
            session_id=name,
            trial_paths=TrialPaths(trial_dir=output),
            task_env_config=config,
            network_policy=NetworkPolicy(network_mode="no-network"),
            app_name="repo2rlenv-owned-generation",
            sandbox_timeout_secs=900,
        )
        await asyncio.wait_for(environment.start(force_build=False), timeout=900)
        check = (
            "import json, torch; "
            f"assert torch.cuda.device_count() == {gpus}; "
            "x=torch.ones(4,device='cuda'); assert (x+x).sum().item()==8; "
            "print(json.dumps({'torch':torch.__version__,'cuda':torch.version.cuda,'devices':[torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())]}))"
        )
        device = await environment.exec(
            "python -c " + shlex.quote(check), cwd="/workspace", timeout_sec=60
        )
        (output / "device.stdout").write_text(device.stdout)
        (output / "device.stderr").write_text(device.stderr)
        if device.return_code:
            raise ValueError("Native GPU allocation/import check failed; see device logs")
        freeze = await environment.exec("python -m pip freeze", cwd="/workspace", timeout_sec=30)
        (output / "pip-freeze.txt").write_text(freeze.stdout)
        if not tests:
            return {"device": json.loads(device.stdout), "image_id": environment._image.object_id}
        command = [
            "python",
            "-m",
            "pytest",
            "-o",
            "addopts=",
            *(profile.options.test_selectors or profile.options.test_paths),
            "-q",
            "--tb=short",
            "--junitxml=/tmp/results.xml",
        ]
        result = await environment.exec(
            shlex.join(command), cwd="/workspace", timeout_sec=profile.options.test_timeout_sec
        )
        (output / "stdout.txt").write_text(result.stdout)
        (output / "stderr.txt").write_text(result.stderr)
        try:
            await environment.download_file("/tmp/results.xml", output / "results.xml")
        except Exception as exc:
            raise ValueError(
                f"Native pytest produced no readable report (exit {result.return_code}); "
                "inspect the saved stdout.txt and stderr.txt"
            ) from exc
        parsed = parse_junit((output / "results.xml").read_text(), returncode=result.return_code)
        save_record(
            output / "results.json", {"returncode": parsed.returncode, "statuses": parsed.statuses}
        )
        return parsed
    finally:
        try:
            if environment is not None:
                await environment.stop()
        finally:
            ACCOUNTING.reset(token)


def build_context(base: Path, profile: Profile, destination: Path, *, public: bool = False) -> Path:
    """Copy source data and an explicit recipe; never build on this machine."""
    destination.mkdir(parents=True, exist_ok=False)
    for path in sorted(base.rglob("*")):
        if path.is_symlink():
            raise ValueError("Native contexts cannot contain source symlinks")
        if not path.is_file():
            continue
        relative = PurePosixPath(path.relative_to(base).as_posix())
        if public and private_asset(relative, profile.options):
            continue
        target = destination / "source" / str(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    (destination / "Dockerfile").write_text(repository_build(profile.options))
    return destination


class NativeStages:
    def __init__(self, runner):
        self.runner = runner

    def begin(self, output: Path, inputs: dict):
        identity = {
            "inputs": inputs,
            "gpus": self.runner.options.gpus,
            "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        manifest = output / "inputs.json"
        if output.exists():
            if not manifest.is_file() or json.loads(manifest.read_text()) != identity:
                raise ValueError(
                    "Native stage inputs changed; preserve its evidence and use a new attempt"
                )
            result_path = output / "stage-result.json"
            if not result_path.is_file():
                raise ValueError(
                    "Native stage is incomplete; reconcile existing allocations before resuming"
                )
            result = json.loads(result_path.read_text())
            if (
                result.get("bundle_hash")
                and task_identity(output / result["value"]["task_relative"])
                != result["bundle_hash"]
            ):
                raise ValueError("Native constructed task changed after recording")
            return result
        output.mkdir(parents=True)
        save_record(manifest, identity)
        return None

    def bootstrap(self, root, key, source, profile, checkout):
        output = root / "native" / key
        existing = self.begin(output, {"source": source, "profile": profile})
        if existing is not None:
            return existing
        prepared = self.runner.remote(
            root,
            key + "-snapshot",
            {"stage": "prepare_native", "source": source, "profile": profile, "checkout": checkout},
        )
        if prepared["status"] != "completed":
            save_record(output / "stage-result.json", prepared)
            return prepared
        profile = Profile.model_validate(profile)
        base = Path(prepared["local"]) / prepared["value"]["base_relative"]
        try:
            validate_readiness_paths(base, profile.options)
            context = build_context(base, profile, output / "merged-context")
            healthy = asyncio.run(
                check_context(
                    context,
                    profile,
                    output / "readiness",
                    self.runner.budget,
                    self.runner.options.gpus,
                )
            )
            if healthy.returncode or not healthy.passed:
                raise ValueError("Merged-head native GPU readiness failed")
            public = build_context(base, profile, output / "public-context", public=True)
            public_result = asyncio.run(
                check_context(
                    public,
                    profile,
                    output / "public-readiness",
                    self.runner.budget,
                    self.runner.options.gpus,
                    tests=False,
                )
            )
            result = {
                "status": "completed",
                "value": {
                    "base": str(base),
                    "defective": str(
                        Path(prepared["local"]) / prepared["value"]["defective_relative"]
                    ),
                    "removed": prepared["value"]["removed"],
                    "readiness": healthy.statuses,
                    "public_image": public_result,
                    "dependency_cache": {
                        "cache_hit": None,
                        "scope": "native Modal Dockerfile layers; hit status not reported",
                    },
                },
            }
        except Exception as exc:
            result = self.failure(output, exc)
        save_record(output / "stage-result.json", result)
        return result

    def construct(self, root, key, source, profile, design, ready):
        output = root / "native" / key
        existing = self.begin(
            output, {"source": source, "profile": profile, "design": design, "ready": ready}
        )
        if existing is not None:
            return existing
        profile, design = Profile.model_validate(profile), Design.model_validate(design)
        base, defective = Path(ready["base"]), Path(ready["defective"])
        options = profile.options.model_copy(deep=True)
        additions = {}
        try:
            if design.upstream_test_policy == "replace":
                options.test_selectors = []
            if design.additional_tests.strip():
                relative = "tests/tasksmith_behavior.py"
                if (base / relative).exists() or not (base / "tests").is_dir():
                    raise ValueError(
                        "Private supplements need an unused tests/tasksmith_behavior.py path"
                    )
                additions[relative] = design.additional_tests.encode()
                options.test_selectors.append(relative)
                if "tests" not in options.test_paths:
                    options.test_paths.append("tests")
            grading = profile.model_copy(update={"options": options})
            merged = build_context(base, grading, output / "merged-context")
            for name, content in additions.items():
                path = merged / "source" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            healthy = asyncio.run(
                check_context(
                    merged,
                    grading,
                    output / "healthy",
                    self.runner.budget,
                    self.runner.options.gpus,
                )
            )
            if healthy.returncode or not healthy.passed:
                raise ValueError("Designed GPU verifier fails on the actual PR reference")
            broken_context = output / "broken-context"
            shutil.copytree(merged, broken_context)
            for name in source["source_files"]:
                destination = broken_context / "source" / name
                if name in ready["removed"]:
                    destination.unlink()
                else:
                    destination.write_bytes((defective / name).read_bytes())
            broken = asyncio.run(
                check_context(
                    broken_context,
                    grading,
                    output / "defective",
                    self.runner.budget,
                    self.runner.options.gpus,
                )
            )
            contrast = execution_contrast(healthy, broken)
            if not contrast["PASS_TO_PASS"]:
                raise ValueError("GPU verifier needs an unchanged adjacent behavior case")
            task = export_repository_task(
                base=base,
                defective={
                    name: None if name in ready["removed"] else (defective / name).read_bytes()
                    for name in source["source_files"]
                },
                reference={name: (base / name).read_bytes() for name in source["source_files"]},
                options=options,
                instruction=design.instruction,
                destination=output / "task",
                name="tasksmith-" + source["id"],
                org="repo2rlenv",
                contrast=contrast,
                metadata={
                    "recipe": "tasksmith",
                    "recipe_version": "1",
                    "source_url": source["url"],
                    "source_head": source["head"],
                    "source_base": source["base"],
                    "workspace_strategy": source["workspace_strategy"],
                    "source_diff_sha256": hashlib.sha256(
                        source["source_diff"].encode()
                    ).hexdigest(),
                    "acceptance_profile": "practical-generation-v1",
                    "upstream_test_policy": design.upstream_test_policy,
                },
                verifier_source=additions,
                collect_source_directories=True,
            )
            config_path = task / "task.toml"
            config = tomllib.loads(config_path.read_text())
            for environment in (config["environment"], config["verifier"]["environment"]):
                environment.update(
                    gpus=self.runner.options.gpus, gpu_types=["L4"], cpus=4, memory_mb=16384
                )
            config_path.write_text(tomli_w.dumps(config))
            identity = refresh_identity(task)
            result = {
                "status": "completed",
                "bundle_hash": identity,
                "local": str(output),
                "value": {
                    "task_relative": str(task.relative_to(output)),
                    "contrast": contrast,
                    "options": options.model_dump(),
                    "strategy": design.strategy,
                    "test_evidence": test_excerpts(
                        base,
                        contrast["FAIL_TO_PASS"],
                        additional_sources={
                            name: content.decode() for name, content in additions.items()
                        },
                    ),
                },
            }
        except Exception as exc:
            result = self.failure(output, exc)
        save_record(output / "stage-result.json", result)
        return result

    @staticmethod
    def failure(output: Path, exc: Exception) -> dict:
        for path in output.glob("**/allocations/*.json"):
            if not path.name.endswith(".cost.json"):
                record = json.loads(path.read_text())
                if record["state"] == "reservation_failed" and isinstance(exc, BudgetExceeded):
                    # Reservation precedes provider dispatch. Retain the receipt,
                    # but do not turn a known budget denial into an uncertain create.
                    continue
                if record["state"] not in {"terminated", "build_failed"}:
                    raise RuntimeError(
                        f"Reconcile native allocation before another attempt: {path}"
                    ) from exc
        if isinstance(exc, BudgetExceeded):
            raise exc
        from modal.exception import ImageBuildError

        if isinstance(exc, ImageBuildError):
            try:
                logs = subprocess.run(
                    [sys.executable, "-m", "modal", "image", "logs", exc.image_id],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=60,
                    check=False,
                )
                (output / "image-build.stderr.txt").write_text(logs.stdout + logs.stderr)
            except (OSError, subprocess.TimeoutExpired):
                (output / "image-build.stderr.txt").write_text(
                    "Build log retrieval failed; image_id=" + exc.image_id
                )
        return {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "logs": {
                str(path.relative_to(output)): path.read_text(errors="replace")[-8000:]
                for pattern in (
                    "**/stdout.txt",
                    "**/*stderr.txt",
                    "**/device.stderr",
                    "**/results.json",
                )
                for path in output.glob(pattern)
            },
        }
