"""Task-aware static checks behind `repo2rlenv validate --deep` / `--oracle`.

The default `validate` pass only proves each task.toml parses and names its
task. These checks go one level deeper: they read the task's assets and its
`[metadata.repo2env]` table and report what a Harbor run or a trainer would
trip over — a missing instruction, a test_execution task with no test entry
point, a graded verifier whose F2P list isn't JSON, a reproducibility mode
nobody writes.

The rules mirror what Harbor and our emitter actually do, so legitimate task
variants don't fail:

- Harbor accepts `[environment].docker_image`, `environment/Dockerfile` or
  `environment/docker-compose.yaml` as the environment definition
  (`harbor.environments.definition.has_agent_environment_definition`).
- Harbor picks `.sh` scripts for Linux tasks and `.bat` for Windows ones, and
  runs `chmod +x` itself — so the executable bit is never checked.
- Harbor solutions are optional; the `solution/` oracle is only required with
  `--oracle`, and only for Repo2RLEnv tasks.
- `pr_diff` with `emit_harbor_env=False` is text-only by design: no
  environment/ and no tests/ is valid when the task declares no
  `test_execution` reward.

Limits: this is static validation. It cannot tell whether a Dockerfile builds,
whether a shell script's dependencies exist, or whether the oracle patch applies
at `base_commit` — `harbor run --agent oracle` is still the ground truth. Only
the verifier assets Repo2RLEnv itself emits are checked in detail. Multi-step
(`[[steps]]`) Harbor tasks skip the asset-layout checks.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

from repo2rlenv.registry.integration import ReproMode
from repo2rlenv.spec.input import PipelineName

Severity = Literal["error", "warning"]

PIPELINES = frozenset(p.value for p in PipelineName)
REPRO_MODES = frozenset(get_args(ReproMode))
IMAGE_VISIBILITIES = frozenset({"public", "private", "unknown"})
INLINE_RECIPE_SOURCES = frozenset({"user_dockerfile", "agent_replay"})
REWARD_KINDS = frozenset({"test_execution", "diff_similarity"})
# Early v0.8.3 pr_diff emitted this before it was renamed to the spec's
# `diff_similarity` (commit 5125778); published datasets still carry it.
_LEGACY_REWARD_KINDS = {"diff_similarity_multi_component": "diff_similarity"}

# Pipelines whose graded test.sh reads tests/{verifier.py,f2p.json,p2p.json}
# (written by `pipelines.pr_runtime._runtime_aux_files`) whenever the
# pipeline subtable carries a non-empty `fail_to_pass`.
GRADED_VERIFIER_PIPELINES = frozenset({"pr_runtime", "commit_runtime", "cve_patches"})
# Pipelines that ship an LLM-authored test at tests/<subtable.test_filename>.
TEST_FILE_PIPELINES = frozenset({"code_instruct", "equivalence_tests"})

# Pipelines whose runnable test.sh reads tests/{verifier.py,oracle.patch,
# instruction.md} — shipped as plain aux files (by `pipelines.pr_diff.
# _pr_diff_aux_files`) so the oracle stays out of the agent's image.
DIFF_VERIFIER_PIPELINES = frozenset({"pr_diff"})

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FROM_LINE_RE = re.compile(r"^\s*FROM\s+(\S+)", re.IGNORECASE | re.MULTILINE)
_DIFF_FILE_HEADER_RE = re.compile(r"^(diff --git |\+\+\+ )", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Finding:
    severity: Severity
    path: str  # task-relative POSIX path the finding is about
    message: str


class _Report:
    def __init__(self) -> None:
        self.findings: list[Finding] = []

    def error(self, path: str, message: str) -> None:
        self.findings.append(Finding("error", path, message))

    def warn(self, path: str, message: str) -> None:
        self.findings.append(Finding("warning", path, message))


def validate_task(task_dir: Path, data: dict[str, Any], *, oracle: bool = False) -> list[Finding]:
    """Statically check one task directory against its parsed task.toml.

    `data` is the already-parsed task.toml. With `oracle=True`, Repo2RLEnv
    tasks must also ship a solve script. Native pipelines require their patch;
    named recipes can solve through scripts without a patch artifact.
    """
    report = _Report()

    env_cfg = data.get("environment", {})
    if not isinstance(env_cfg, dict):
        report.error("task.toml", "[environment] must be a table")
        env_cfg = {}
    task_os = env_cfg.get("os", "linux")
    if task_os not in ("linux", "windows"):
        report.error("task.toml", f"[environment].os must be 'linux' or 'windows', got {task_os!r}")
        task_os = "linux"
    script_ext = ".bat" if task_os == "windows" else ".sh"

    metadata = data.get("metadata", {})
    r2e = metadata.get("repo2env") if isinstance(metadata, dict) else None
    if r2e is not None and not isinstance(r2e, dict):
        report.error("task.toml", "[metadata.repo2env] must be a table")
        r2e = None

    if data.get("steps"):
        report.warn(
            "task.toml",
            "multi-step [[steps]] task: per-step instruction/tests/solution are not checked",
        )
    else:
        _check_layout(task_dir, env_cfg, r2e, script_ext, report)

    if r2e is not None:
        _check_repro(task_dir, r2e, report)
        if oracle and not data.get("steps"):
            _check_oracle(task_dir, r2e, script_ext, report)

    return report.findings


def _check_layout(
    task_dir: Path,
    env_cfg: dict[str, Any],
    r2e: dict[str, Any] | None,
    script_ext: str,
    report: _Report,
) -> None:
    _check_nonempty_file(task_dir, "instruction.md", report)

    env_dir = task_dir / "environment"
    has_env_definition = bool(env_cfg.get("docker_image")) or any(
        (env_dir / name).is_file() for name in ("Dockerfile", "docker-compose.yaml")
    )
    test_script = f"tests/test{script_ext}"
    has_tests_dir = (task_dir / "tests").is_dir()

    if r2e is None:
        # Non-Repo2RLEnv task: we don't know its conventions, so only flag
        # directories that exist but can't be used.
        if env_dir.is_dir() and not has_env_definition:
            report.error("environment", _no_env_definition_msg())
        if has_tests_dir:
            _check_nonempty_file(task_dir, test_script, report)
        return

    reward_kinds = _reward_kinds(r2e, report)
    pipeline = r2e.get("pipeline")
    if pipeline is not None and not _one_of(pipeline, PIPELINES):
        report.warn(
            "task.toml",
            f"unknown pipeline {pipeline!r}; pipeline-specific asset checks skipped",
        )

    runnable = (
        "test_execution" in reward_kinds or has_env_definition or env_dir.is_dir() or has_tests_dir
    )
    if runnable:
        if not has_env_definition:
            report.error("environment", _no_env_definition_msg())
        _check_nonempty_file(task_dir, test_script, report)
    elif "diff_similarity" in reward_kinds:
        # Text-only task (pr_diff with emit_harbor_env=False): the patch is
        # the whole reward oracle, so it's required even without --oracle.
        _check_nonempty_file(task_dir, "solution/patch.diff", report)

    sub = r2e.get(pipeline) if isinstance(pipeline, str) else None
    if not isinstance(sub, dict):
        return
    if pipeline in GRADED_VERIFIER_PIPELINES and sub.get("fail_to_pass"):
        _check_graded_verifier(task_dir, sub["fail_to_pass"], report)
    if pipeline in TEST_FILE_PIPELINES and "test_filename" in sub:
        _check_test_file(task_dir, sub["test_filename"], report)
    if pipeline in DIFF_VERIFIER_PIPELINES and has_env_definition:
        _check_diff_verifier(task_dir, report)


def _reward_kinds(r2e: dict[str, Any], report: _Report) -> list[str]:
    kinds = r2e.get("reward_kinds")
    if kinds is None:
        report.warn("task.toml", "[metadata.repo2env] has no reward_kinds")
        return []
    if not isinstance(kinds, list) or not all(isinstance(k, str) for k in kinds):
        report.error("task.toml", "[metadata.repo2env].reward_kinds must be a list of strings")
        return []
    kinds = [_LEGACY_REWARD_KINDS.get(k, k) for k in kinds]
    for k in kinds:
        if k not in REWARD_KINDS:
            report.warn("task.toml", f"unknown reward kind {k!r}")
    return kinds


def _check_graded_verifier(task_dir: Path, meta_f2p: Any, report: _Report) -> None:
    verifier = _check_nonempty_file(task_dir, "tests/verifier.py", report)
    if verifier is not None:
        try:
            ast.parse(verifier, filename="tests/verifier.py")
        except SyntaxError as exc:
            report.error("tests/verifier.py", f"not valid Python: {exc.msg} (line {exc.lineno})")

    f2p = _load_test_id_list(task_dir, "tests/f2p.json", report)
    if f2p is not None:
        if not f2p:
            report.error("tests/f2p.json", "empty FAIL_TO_PASS list on a graded task")
        elif _is_str_list(meta_f2p) and set(f2p) != set(meta_f2p):
            report.warn("tests/f2p.json", "does not match fail_to_pass in task.toml")
    _load_test_id_list(task_dir, "tests/p2p.json", report)


def _check_diff_verifier(task_dir: Path, report: _Report) -> None:
    """A runnable pr_diff task must ship its verifier + oracle under tests/.

    The oracle is deliberately NOT in the environment image (an agent could
    read and apply it), so it rides in tests/, which Harbor delivers only at
    verify time. If these are missing the task builds but scores nothing.
    """
    verifier = _check_nonempty_file(task_dir, "tests/verifier.py", report)
    if verifier is not None:
        try:
            ast.parse(verifier, filename="tests/verifier.py")
        except SyntaxError as exc:
            report.error("tests/verifier.py", f"not valid Python: {exc.msg} (line {exc.lineno})")
    oracle = _check_nonempty_file(task_dir, "tests/oracle.patch", report)
    if oracle is not None and not _DIFF_FILE_HEADER_RE.search(oracle):
        report.error("tests/oracle.patch", "does not look like a unified diff")
    _check_nonempty_file(task_dir, "tests/instruction.md", report)


def _load_test_id_list(task_dir: Path, rel: str, report: _Report) -> list[str] | None:
    path = task_dir / rel
    if not path.is_file():
        report.error(rel, "missing")
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        report.error(rel, f"not valid JSON: {exc}")
        return None
    if not _is_str_list(value):
        report.error(rel, "must be a JSON list of test-name strings")
        return None
    return value


def _check_test_file(task_dir: Path, test_filename: Any, report: _Report) -> None:
    bare = isinstance(test_filename, str) and re.fullmatch(r"[^/\\]+", test_filename)
    if not bare or test_filename in (".", ".."):
        report.error("task.toml", f"test_filename must be a bare filename, got {test_filename!r}")
        return
    _check_nonempty_file(task_dir, f"tests/{test_filename}", report)


def _check_repro(task_dir: Path, r2e: dict[str, Any], report: _Report) -> None:
    dockerfile = task_dir / "environment" / "Dockerfile"
    repro = r2e.get("reproducibility")
    if repro is None:
        if dockerfile.is_file() and _version_at_least(r2e.get("spec_version"), (0, 2, 0)):
            report.warn(
                "task.toml",
                "spec_version >= 0.2.0 task with environment/Dockerfile has no "
                "[metadata.repo2env.reproducibility]",
            )
        return
    if not isinstance(repro, dict):
        report.error("task.toml", "[metadata.repo2env.reproducibility] must be a table")
        return

    where = "[metadata.repo2env.reproducibility]"
    mode = repro.get("mode")
    if not _one_of(mode, REPRO_MODES):
        supported = ", ".join(sorted(REPRO_MODES))
        report.error("task.toml", f"{where}.mode must be one of {supported}; got {mode!r}")

    for key in ("image_ref", "image_tag", "pushed_at", "pushed_by", "fallback_reason"):
        if key in repro and not isinstance(repro[key], str):
            report.error("task.toml", f"{where}.{key} must be a string")
    visibility = repro.get("image_visibility")
    if visibility is not None and not _one_of(visibility, IMAGE_VISIBILITIES):
        report.error(
            "task.toml",
            f"{where}.image_visibility must be one of public, private, unknown; got {visibility!r}",
        )

    if mode == "registry":
        image_ref = repro.get("image_ref")
        if not isinstance(image_ref, str) or not image_ref.strip():
            report.error("task.toml", f"{where}.image_ref is required for mode='registry'")
        elif dockerfile.is_file():
            m = _FROM_LINE_RE.search(dockerfile.read_text(encoding="utf-8", errors="replace"))
            if m and m.group(1) != image_ref:
                report.warn(
                    "environment/Dockerfile",
                    f"FROM {m.group(1)} does not match reproducibility.image_ref {image_ref}",
                )
    elif mode == "inline_dockerfile":
        if not dockerfile.is_file():
            report.error(
                "environment/Dockerfile", "missing; mode='inline_dockerfile' bakes the recipe here"
            )
        source = repro.get("inline_recipe_source")
        if source is not None and not _one_of(source, INLINE_RECIPE_SOURCES):
            report.error(
                "task.toml",
                f"{where}.inline_recipe_source must be user_dockerfile or agent_replay; "
                f"got {source!r}",
            )
        sha = repro.get("inline_recipe_sha256")
        if sha is not None and not (isinstance(sha, str) and _SHA256_RE.match(sha)):
            report.error("task.toml", f"{where}.inline_recipe_sha256 must be 'sha256:<64 hex>'")
        lines = repro.get("inline_recipe_lines")
        if lines is not None and (
            isinstance(lines, bool) or not isinstance(lines, int) or lines < 0
        ):
            report.error("task.toml", f"{where}.inline_recipe_lines must be a non-negative integer")


def _check_oracle(task_dir: Path, r2e: dict[str, Any], script_ext: str, report: _Report) -> None:
    # Named recipes can restore files or run commands directly in solve.sh.
    # Preserve the stricter patch contract for existing native tasks, and
    # validate any patch a recipe actually includes.
    requires_patch = r2e.get("recipe", "native") == "native"
    patch = None
    if requires_patch or (task_dir / "solution/patch.diff").exists():
        patch = _check_nonempty_file(task_dir, "solution/patch.diff", report)
    pr_diff = r2e.get("pr_diff")
    search_replace = isinstance(pr_diff, dict) and pr_diff.get("diff_format") == "search_replace"
    if patch is not None and not search_replace and not _DIFF_FILE_HEADER_RE.search(patch):
        report.error(
            "solution/patch.diff", "not a unified diff (no 'diff --git' or '+++' file header)"
        )
    _check_nonempty_file(task_dir, f"solution/solve{script_ext}", report)


def _check_nonempty_file(task_dir: Path, rel: str, report: _Report) -> str | None:
    """Report a missing/unreadable/blank file; return its text when usable."""
    path = task_dir / rel
    if not path.is_file():
        report.error(rel, "missing")
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        report.error(rel, f"not valid UTF-8: {exc}")
        return None
    if not text.strip():
        report.error(rel, "empty")
        return None
    return text


def _one_of(value: Any, allowed: frozenset[str]) -> bool:
    # isinstance first: TOML arrays/tables are unhashable and would raise on `in`.
    return isinstance(value, str) and value in allowed


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _no_env_definition_msg() -> str:
    return (
        "no environment definition: add environment/Dockerfile or "
        "environment/docker-compose.yaml, or set [environment].docker_image"
    )


def _version_at_least(value: Any, minimum: tuple[int, ...]) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return tuple(int(part) for part in value.split(".")) >= minimum
    except ValueError:
        return False
