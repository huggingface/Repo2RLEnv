"""Reviewed, process-local CPU selection for unchanged upstream TRL tests.

This module supplies an independently reviewed resource adaptation to bootstrap.
Approval does not establish readiness: the controller must preserve its original
budget/deadline, run every frozen check remotely, and retain complete collection
and execution coverage on the same image.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import stat
import time
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from repo2rlenv.tasksmith import review, worker
from repo2rlenv.tasksmith.authoring import Discovery
from repo2rlenv.tasksmith.models import Digest, GitSHA, PRIdentity, StrictModel, safe_relative
from repo2rlenv.tasksmith.worker import canonical_digest, save_json

POLICY_VERSION = 1
MAX_INPUT_BYTES = 384_000
ConfigName = Literal["DPOConfig", "RewardConfig", "SFTConfig"]
CONFIG_MODULES = {
    "DPOConfig": "trl.trainer.dpo_config",
    "RewardConfig": "trl.trainer.reward_config",
    "SFTConfig": "trl.trainer.sft_config",
}
DOC_URLS = (
    "https://huggingface.co/docs/transformers/v4.56.2/en/main_classes/trainer",
    "https://huggingface.co/docs/transformers/v4.56.2/en/perf_train_cpu",
)
DOCUMENTED_CONTRACT = """Transformers 4.56.2 documents use_cpu=False by default;
use_cpu selects CPU execution. Its CPU guide explicitly demonstrates
TrainingArguments(bf16=True, use_cpu=True). CPU selection does not require changing
bf16, precision, seeds, model inputs, assertions, or expected results. This is an
API capability, not evidence that every selected upstream test can run on CPU.
Sources: """ + "\n".join(DOC_URLS)


class CpuFixtureUnsupported(RuntimeError):
    """No faithful approved CPU adaptation; retain evidence and stop."""


class CapturedSource(StrictModel):
    """Complete source text captured by the controller from the pinned HEAD."""

    path: str
    sha256: Digest
    size: int = Field(ge=0, le=192_000, strict=True)
    text: str

    _path = field_validator("path")(safe_relative)

    @model_validator(mode="after")
    def complete(self):
        raw = self.text.encode()
        if len(raw) != self.size or hashlib.sha256(raw).hexdigest() != self.sha256:
            raise ValueError("Captured source is incomplete or its hash changed")
        return self


class CpuFixtureRequest(StrictModel):
    source: PRIdentity
    base_sha: GitSHA
    head_sha: GitSHA
    discovery: Discovery
    profile: Literal["cpu-direct"] = "cpu-direct"
    transformers_version: Literal["4.56.2"] = "4.56.2"
    constructors: list[ConfigName] = Field(min_length=1, max_length=3)
    source_files: list[CapturedSource] = Field(min_length=2, max_length=24)
    readiness: dict

    @model_validator(mode="after")
    def bounded_request(self):
        from repo2rlenv.tasksmith.build import _profile_failure, _pytest_command

        if len(self.model_dump_json().encode()) > MAX_INPUT_BYTES:
            raise ValueError("CPU adaptation evidence exceeds complete-input bound")
        if self.source.repository != "huggingface/trl":
            raise ValueError("This bounded adapter supports the reviewed TRL config API only")
        if len(set(self.constructors)) != len(self.constructors):
            raise ValueError("Duplicate constructors")
        paths = {item.path for item in self.source_files}
        if len(paths) != len(self.source_files):
            raise ValueError("Duplicate captured source paths")
        if not 1 <= len(self.discovery.upstream_test_commands) <= 8:
            raise ValueError("CPU adaptation requires the complete bounded upstream list")
        required = {CONFIG_MODULES[name].replace(".", "/") + ".py" for name in self.constructors}
        required.add("trl/trainer/base_config.py")
        for command in self.discovery.upstream_test_commands:
            _, modules = _pytest_command(command)
            required.update(modules)
        if required - paths:
            raise ValueError(f"Missing complete pinned module source: {sorted(required - paths)}")
        rows = self.readiness.get("checks", [])
        if (
            self.readiness.get("passed") is not False
            or any(
                not isinstance(error, str)
                or not error.startswith(
                    (
                        "Changed upstream tests were not executed:",
                        "Corrected command executed no non-skipped tests:",
                    )
                )
                for error in self.readiness.get("execution_errors", [])
            )
            or not isinstance(rows, list)
            or not rows
            or any(not isinstance(row, dict) for row in rows)
            or not isinstance(self.readiness.get("reset"), dict)
            or self.readiness["reset"].get("exit_code") != 0
        ):
            raise ValueError("Adaptation requires retained failed readiness and successful reset")
        failed = rows[-1]
        commands = [
            f"git checkout --detach {self.head_sha} && python -m pip install --no-index --no-deps --no-build-isolation -e .",
            *self.discovery.readiness_commands,
            *self.discovery.upstream_test_commands,
        ]
        if (
            [row.get("command") for row in rows] != commands[: len(rows)]
            or len(rows) > len(commands)
            or failed.get("command") not in self.discovery.upstream_test_commands
            or type(failed.get("exit_code")) is not int
            or failed["exit_code"] <= 0
            or failed.get("timed_out")
            or not (failed.get("stdout") or failed.get("stderr"))
            or any(row.get("exit_code") != 0 for row in rows[:-1])
        ):
            raise ValueError("Need an actual upstream configuration failure, not a timeout")
        if _profile_failure(self.discovery, self.readiness) is None:
            raise ValueError("No observed CPU/GPU capability exception supports this adapter")
        return self


class Citation(StrictModel):
    evidence_id: str
    quote: str = Field(min_length=10, max_length=600)


class CommandAssessment(StrictModel):
    command: str
    cpu_semantics_preserved: bool
    explanation: str = Field(min_length=20)
    evidence: list[Citation] = Field(min_length=1, max_length=4)


class CpuFixtureAssessment(StrictModel):
    approved: bool
    essential_gpu_semantics: bool
    explanation: str = Field(min_length=30)
    commands: list[CommandAssessment] = Field(min_length=1, max_length=8)
    evidence: list[Citation] = Field(min_length=2, max_length=8)

    @model_validator(mode="after")
    def coherent(self):
        if self.approved and (
            self.essential_gpu_semantics
            or not all(c.cpu_semantics_preserved for c in self.commands)
        ):
            raise ValueError("Essential GPU semantics or unsupported commands cannot be approved")
        return self


REVIEW = """Judge one fixed resource adaptation, not a test rewrite: the host runner
injects use_cpu=True ONLY when omitted from allowlisted TRL config constructors.
Explicit use_cpu=False aborts adaptation. It changes no bf16/precision settings,
assertions, skips, test selection, source files, dependency recipe, or expected data.
Read the complete selected test modules and configuration source, original failure,
and documented API. For EACH unchanged upstream command decide whether the observed
behavior and assertions remain meaningful on CPU. Inspect constructor call sites,
fixtures, decorators/skip conditions, device checks, precision and distributed/GPU
requirements. Reject essential GPU behavior, unsupported precision, explicit GPU
choices, altered skip coverage, incomplete source, and unknown cases. Source-supported
CPU eligibility is not proof of runtime success. Cite failure and source evidence;
docs alone cannot prove fixture equivalence. Do not propose monkeypatch code or
weaken checks. Approving merely permits a full fresh unchanged-check remote rerun.
"""


def _evidence(request):
    return {
        "request": request.model_dump_json(exclude={"source_files", "readiness"}),
        "failure": json.dumps(request.readiness, ensure_ascii=False),
        "documented_api": DOCUMENTED_CONTRACT,
        **{f"source/{item.path}": item.text for item in request.source_files},
    }


def _validate_assessment(value, request, evidence):
    from repo2rlenv.tasksmith.build import _pytest_command

    if [row.command for row in value.commands] != request.discovery.upstream_test_commands:
        raise ValueError("Review must cover every unchanged command in its original order")
    citations = list(value.evidence)
    for row in value.commands:
        citations.extend(row.evidence)
        _, modules = _pytest_command(row.command)
        if {"source/" + path for path in modules} - {c.evidence_id for c in row.evidence}:
            raise ValueError("Each command needs support from its actual selected test modules")
    for citation in citations:
        if (
            citation.evidence_id not in evidence
            or citation.quote not in evidence[citation.evidence_id]
        ):
            raise ValueError("Review citations must quote exact retained evidence")
    if "failure" not in {c.evidence_id for c in value.evidence}:
        raise ValueError("Review must explain the actual configuration failure")


def _inputs(request, config, budget, deadline, root):
    from repo2rlenv.tasksmith import build

    # Revalidate even a model_copy-created request; copies skip Pydantic validators.
    request = CpuFixtureRequest.model_validate(request.model_dump(mode="json"))
    if not math.isfinite(deadline):
        raise ValueError("Original absolute deadline must be finite")
    return {
        "policy": POLICY_VERSION,
        "root": str(root.absolute()),
        "request": request.model_dump(mode="json"),
        "config": config.model_dump(mode="json"),
        "budget": {
            "path": str(budget.path.resolve()),
            **{
                key: getattr(budget, key)
                for key in ("limit", "scope", "scope_limit", "group", "group_limit")
            },
        },
        "deadline": deadline,
        "implementation": {
            key: hashlib.sha256(Path(path).read_bytes()).hexdigest()
            for key, path in {
                "adapter": __file__,
                "bootstrap_contract": build.__file__,
                "review": review.__file__,
                "worker": worker.__file__,
            }.items()
        },
    }


def _inventory(root):
    files = {}
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise CpuFixtureUnsupported("Nonregular CPU approval evidence")
        relative = path.relative_to(root).as_posix()
        if stat.S_ISREG(mode) and relative not in {"phase.json", "result.json"}:
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def _root(root):
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise CpuFixtureUnsupported("Linked CPU approval root")


def _completed(root, identity, request):
    _root(root)
    phase_path = root / "phase.json"
    if phase_path.is_symlink() or not stat.S_ISREG(phase_path.stat().st_mode):
        raise CpuFixtureUnsupported("Nonregular CPU approval phase")
    phase = json.loads(phase_path.read_text())
    if phase.get("input_digest") != identity or phase.get("status") != "completed":
        raise CpuFixtureUnsupported("CPU approval identity changed or needs reconciliation")
    result_path = root / "result.json"
    if result_path.is_symlink() or hashlib.sha256(
        result_path.read_bytes()
    ).hexdigest() != phase.get("result_sha256"):
        raise CpuFixtureUnsupported("CPU approval result changed")
    result = json.loads(result_path.read_text())
    if result["evidence_files"] != _inventory(root) or result["input_digest"] != identity:
        raise CpuFixtureUnsupported("CPU approval evidence changed")
    value = CpuFixtureAssessment.model_validate(result["assessment"])
    _validate_assessment(value, request, _evidence(request))
    if not value.approved:
        raise CpuFixtureUnsupported("CPU fixture adaptation rejected: " + value.explanation)
    return result


async def review_cpu_fixture(request, *, config, budget, deadline, root: Path) -> dict:
    """One read-only independent review, charged to the existing Budget unchanged.

    Completed rejections cannot be rerolled; interrupted reviews require explicit
    reconciliation. The receipt permits runner generation, never claims readiness.
    """
    inputs = _inputs(request, config, budget, deadline, root)
    identity = canonical_digest(inputs)
    _root(root)
    if (root / "phase.json").exists():
        return _completed(root, identity, request)
    if root.exists() and any(root.iterdir()):
        raise CpuFixtureUnsupported("Unclaimed CPU review evidence requires reconciliation")
    if deadline <= time.time():
        raise TimeoutError("Original bootstrap deadline exhausted")
    root.mkdir(parents=True, exist_ok=True)
    phase = {"input_digest": identity, "status": "running"}
    with (root / "phase.json").open("x") as stream:
        json.dump(phase, stream)
        stream.flush()
        os.fsync(stream.fileno())
    save_json(root / "inputs.json", inputs)
    evidence = _evidence(request)

    async def validate(value):
        _validate_assessment(value, request, evidence)

    try:
        value = await review._paged(
            schema=CpuFixtureAssessment,
            stage="cpu-fixture-assessment",
            config=config,
            budget=budget,
            root=root / "assessment",
            deadline=deadline,
            evidence=evidence,
            context={"input_digest": identity},
            system=REVIEW,
            validate=validate,
        )
        await validate(value)
        result = {
            "input_digest": identity,
            "assessment": value.model_dump(mode="json"),
            "reference_readiness_passed": False,
            "requires_full_reference_rerun": True,
            "evidence_files": _inventory(root),
        }
        save_json(root / "result.json", result)
        phase.update(
            status="completed",
            result_sha256=hashlib.sha256((root / "result.json").read_bytes()).hexdigest(),
        )
        save_json(root / "phase.json", phase)
    except BaseException as exc:
        phase.update(status="incomplete", error=f"{type(exc).__name__}: {exc}")
        save_json(root / "phase.json", phase)
        raise
    return _completed(root, identity, request)


def _cpu_pytest(classes, argv, pytest_main, events):
    """Trusted runner primitive; tests supply stdlib fake classes and fake pytest.

    Keep this function self-contained: its source is embedded in the remote runner.
    No names, replacement code, or replacement settings come from a model.
    """
    import functools
    import inspect

    originals = []
    try:
        for name, cls in classes.items():
            original = cls.__init__
            signature = inspect.signature(original)
            parameter = signature.parameters.get("use_cpu")
            if (
                parameter is None
                or parameter.default is not False
                or parameter.kind != inspect.Parameter.POSITIONAL_OR_KEYWORD
            ):
                raise RuntimeError("Unsupported use_cpu constructor signature: " + name)

            def wrap(original, signature, name):
                @functools.wraps(original)
                def init(self, *args, **kwargs):
                    bound = signature.bind(self, *args, **kwargs)
                    explicit = "use_cpu" in bound.arguments
                    if explicit and bound.arguments["use_cpu"] is not True:
                        if name not in events["refused"]:
                            events["refused"].append(name)
                        raise RuntimeError("CPU adaptation refuses explicit use_cpu=False: " + name)
                    # Preserve every other explicit/default argument and invoke the
                    # original dataclass constructor (and original post-init) once.
                    if not explicit:
                        kwargs = dict(kwargs, use_cpu=True)
                    counts = events["calls"].setdefault(name, {"injected": 0, "explicit_cpu": 0})
                    counts["explicit_cpu" if explicit else "injected"] += 1
                    return original(self, *args, **kwargs)

                return init

            originals.append((cls, original, "__init__" in cls.__dict__))
            cls.__init__ = wrap(original, signature, name)
        return int(pytest_main(list(argv)))
    finally:
        for cls, original, owned in reversed(originals):
            if owned:
                cls.__init__ = original
            else:
                del cls.__init__


def approved_runtime(request, *, config, budget, deadline, root: Path) -> dict:
    """Revalidate a retained approval before each isolated collection/execution."""
    identity = canonical_digest(_inputs(request, config, budget, deadline, root))
    _completed(root, identity, request)
    if deadline <= time.time():
        raise TimeoutError("Original bootstrap deadline exhausted")
    return {
        "input_digest": identity,
        "head_sha": request.head_sha,
        "deadline": deadline,
        "constructors": {name: CONFIG_MODULES[name] for name in request.constructors},
        "files": {
            item.path: {"sha256": item.sha256, "size": item.size} for item in request.source_files
        },
        "transformers_version": request.transformers_version,
    }


def _cpu_observe(payload, argv, pytest_main):
    """Fixed remote support shared by standalone and coverage-observing runners."""
    import hashlib
    import importlib
    import importlib.metadata
    import inspect
    import os
    import stat
    import subprocess
    import sys
    import time
    from pathlib import Path

    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    events = {
        "input_digest": payload["input_digest"],
        "argv": list(argv),
        "calls": {},
        "refused": [],
        "source_unchanged": False,
    }
    code = 78

    def source_check():
        remaining = payload["deadline"] - time.time()
        if remaining <= 0:
            raise TimeoutError("Original bootstrap deadline exhausted")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=min(10, remaining),
        )
        if head.stdout.strip() != payload["head_sha"]:
            raise RuntimeError("Frozen HEAD changed")
        remaining = payload["deadline"] - time.time()
        if remaining <= 0:
            raise TimeoutError("Original bootstrap deadline exhausted")
        subprocess.run(
            ["git", "diff", "--quiet", "HEAD", "--"], check=True, timeout=min(10, remaining)
        )
        for name, expected in payload["files"].items():
            path = Path(name)
            if any(p.is_symlink() for p in (path, *path.parents)) or not stat.S_ISREG(
                path.stat().st_mode
            ):
                raise RuntimeError("Nonregular frozen source: " + name)
            if path.stat().st_size != expected["size"]:
                raise RuntimeError("Frozen source size changed: " + name)
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected["sha256"]:
                raise RuntimeError("Frozen source changed: " + name)

    try:
        source_check()
        if importlib.metadata.version("transformers") != payload["transformers_version"]:
            raise RuntimeError("Approved Transformers version changed")
        classes = {
            name: getattr(importlib.import_module(module), name)
            for name, module in payload["constructors"].items()
        }
        for name, cls in classes.items():
            expected = Path(payload["constructors"][name].replace(".", "/") + ".py").resolve()
            if Path(inspect.getfile(cls)).resolve() != expected:
                raise RuntimeError("Config did not import from pinned source: " + name)
        code = _cpu_pytest(classes, argv, pytest_main, events)
    except BaseException as exc:
        events["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        try:
            source_check()
            events["source_unchanged"] = True
        except BaseException as exc:
            events["source_error"] = type(exc).__name__ + ": " + str(exc)
            code = 78
        if events["refused"]:
            code = 78
    events["exit_code"] = code
    return code, events


def runtime_source() -> str:
    """Fixed host-authored support only; there is no model-supplied patch payload."""
    return inspect.getsource(_cpu_pytest) + "\n" + inspect.getsource(_cpu_observe)


def render_cpu_pytest(request, command_index, *, config, budget, deadline, root: Path) -> str:
    """Return standalone remote runner source after matching retained approval.

    Launch one fresh remote python -I process with repository CWD and an absolute
    new private receipt path. Controller deadlines and coverage gates still apply.
    """
    from repo2rlenv.tasksmith.build import _pytest_command

    payload = approved_runtime(request, config=config, budget=budget, deadline=deadline, root=root)
    if type(command_index) is not int or not 0 <= command_index < len(
        request.discovery.upstream_test_commands
    ):
        raise ValueError("Invalid frozen upstream command index")
    payload["argv"], _ = _pytest_command(request.discovery.upstream_test_commands[command_index])
    payload["command_index"] = command_index
    return (
        "import json, os, sys\nfrom pathlib import Path\n"
        + "PAYLOAD = json.loads("
        + repr(json.dumps(payload))
        + ")\n"
        + runtime_source()
        + _RUNNER_BODY
    )


_RUNNER_BODY = r"""
receipt = Path(sys.argv[1])
if not receipt.is_absolute() or any(p.is_symlink() for p in (receipt, *receipt.parents)):
    raise RuntimeError('Receipt must be an unlinked absolute private path')
if receipt.is_relative_to(Path.cwd()):
    raise RuntimeError('Receipt must remain outside the frozen repository')
stream = receipt.open('x')
import pytest
code, events = _cpu_observe(PAYLOAD, PAYLOAD['argv'], pytest.main)
events['command_index'] = PAYLOAD['command_index']
json.dump(events, stream, sort_keys=True)
stream.flush()
os.fsync(stream.fileno())
stream.close()
raise SystemExit(code)
"""
