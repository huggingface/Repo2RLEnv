"""Distil the gold `setup.sh` recipe from a bootstrap transcript, then prove it.

The bootstrap phase runs as it does for every other pipeline, but for
`env_setup` its resulting image is discarded — we keep only the transcript
(or, failing that, the reconstruction + rebuild_cmds), `test_cmds`, and
`language`. An LLM distils that noisy raw material into one clean bash
script; that script is then run inside a container built from the EXACT
`environment/Dockerfile` the emitted task ships, and only accepted once the
target test suite goes green in it AND the recipe left the tracked tree
clean. That is what makes `harbor run -a oracle == 1.0` true by construction
rather than by hope.

----------------------------------------------------------------------------
Acknowledgment
----------------------------------------------------------------------------
The "agent-bootstraps-an-environment, then a gold recipe is distilled and
verified from scratch" shape this module implements is informed by:

  Repo2Run (ByteDance, arXiv:2502.13681)
  https://github.com/bytedance/Repo2Run    (Apache-2.0)

  SetupBench (Microsoft, arXiv:2507.09063)
  https://github.com/microsoft/SetupBench    (MIT)

  EnvBench (JetBrains Research, ICLR '25 DL4Code, arXiv:2503.14443)
  https://github.com/JetBrains-Research/EnvBench    (MIT)

  PEP 610 — Recording the Direct URL Origin of Installed Distributions
  https://peps.python.org/pep-0610/

This module is an INDEPENDENT IMPLEMENTATION — no code is copied from any of
the three prior-art repos. It reuses only the general shape (distil a clean
setup recipe, then verify it from a bare state rather than trusting the
agent transcript that produced it) and reimplements it from scratch against
Python stdlib plus this repo's own LLM/Docker primitives. None of the
upstream licenses apply to this file; Repo2RLEnv is Apache-2.0.
----------------------------------------------------------------------------
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from repo2rlenv.bootstrap.docker import DockerError, ExecResult
from repo2rlenv.bootstrap.presets import PRESETS
from repo2rlenv.llm import complete

_FENCE_RE = re.compile(r"```(?:bash|sh|shell)?\n(.*?)```", re.DOTALL)
# A line that looks like a command rather than prose.
_CMD_RE = re.compile(
    r"^\s*(?:#!|set\s|export\s|cd\s|apt-get\s|apt\s|pip\s|python\d?\s|uv\s|npm\s|yarn\s|"
    r"pnpm\s|go\s|cargo\s|make\s|curl\s|bash\s|sh\s|mkdir\s|source\s|\.\s)",
    re.MULTILINE,
)


def extract_script(text: str) -> str | None:
    """Pull the setup script out of an LLM response.

    Prefers the first fenced block; falls back to the raw text when it is
    script-shaped. Returns None for prose with no commands rather than
    shipping an empty recipe that would "verify" as a red suite and burn an
    attempt on a diagnosis we already have.
    """
    if not text or not text.strip():
        return None
    m = _FENCE_RE.search(text)
    body = m.group(1) if m else text
    if not _CMD_RE.search(body):
        return None
    return body if body.endswith("\n") else body + "\n"


def recipe_source_from_bootstrap(bootstrap) -> str | None:
    """The raw material for distillation, in preference order.

    1. The transcript's BASH turns — commands, exit codes, truncated output.
    2. dockerfile_reconstruction + rebuild_cmds when the transcript is absent.
       Neither artifact is usable as-is: the reconstruction replays EVERY BASH
       action including failed attempts, greps, ls-es, and pytest invocations,
       and rebuild_cmds are commands to re-apply a build after a patch, on an
       image where installation already happened.
    3. Neither -> None, and the caller skips `no_recipe_source`.
    """
    path = getattr(bootstrap, "transcript_path", None)
    if path is not None:
        try:
            turns = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            turns = None
        if turns:
            lines = []
            for turn in turns:
                if not isinstance(turn, dict) or turn.get("action") != "BASH":
                    continue
                cmd = str(turn.get("command", "")).strip()
                if not cmd:
                    continue
                rc = turn.get("exit_code")
                out = str(turn.get("output", ""))[:500]
                lines.append(f"$ {cmd}\n[exit {rc}]\n{out}".rstrip())
            if lines:
                return "\n\n".join(lines)

    recon = (getattr(bootstrap, "dockerfile_reconstruction", "") or "").strip()
    rebuild = list(getattr(bootstrap, "rebuild_cmds", None) or [])
    if recon or rebuild:
        parts = []
        if recon:
            parts.append(
                "# Reconstructed Dockerfile (replays every agent command,\n"
                "# successful or not — treat as noisy raw material):\n" + recon
            )
        if rebuild:
            parts.append(
                "# rebuild_cmds (post-patch rebuild, not a from-scratch recipe):\n"
                + "\n".join(rebuild)
            )
        return "\n\n".join(parts)
    return None


class RecipeSandbox(Protocol):
    def exec(self, script: str, *, timeout: int) -> ExecResult: ...
    def put_files(self, files: Mapping[str, str], dest_dir: str) -> None: ...
    def close(self) -> None: ...


@dataclass(slots=True)
class RecipeAttempt:
    script: str
    exit_code: int
    log: str
    tracked_dirty: bool


@dataclass(slots=True)
class RecipeOutcome:
    setup_sh: str | None
    log: str  # the green run's captured suite log
    attempts: int
    cost_usd: float
    setup_time_sec: float
    test_time_sec: float
    skip_reason: str | None  # None on success
    history: list[RecipeAttempt]


_SYSTEM = (
    "You distil a noisy shell transcript into one clean, deterministic setup "
    "script. You output a single bash script and nothing else."
)

_TRACKED_FILE_COMPLAINT = (
    "The previous script modified files tracked in the repository. Do not "
    "modify files tracked in the repository; install from the tree as it is."
)


def _prompt(
    *,
    source: str,
    test_cmds: list[str],
    base_image: str,
    pitfalls: Sequence[str],
    previous: str | None = None,
    failure: str | None = None,
) -> str:
    parts = [
        f"Base image: {base_image}",
        "",
        "Raw material from the bootstrap agent (noisy — includes failed "
        "attempts, diagnostics, and dead ends):",
        source[:20000],
        "",
        "The test suite is run separately by the harness as:",
        " && ".join(test_cmds),
        "",
        "Write ONE bash script, `setup.sh`, that takes this container from the "
        "bare base image to a state where that command runs green. Rules:",
        "- Start with `set -euo pipefail`. It is run from /workspace.",
        "- Include only commands on the successful path. Drop diagnostics and dead ends.",
        "- Do NOT run the test suite; the harness does that separately.",
        "- Consolidate: one `apt-get update && apt-get install -y ...`, not five.",
        "- Preserve ordering constraints the transcript reveals (codegen before "
        "install, version-pretend env vars before an editable install).",
        "- Do NOT `pip install` / `npm install` the repository's own released "
        "package from an index. Install from /workspace.",
        "- Do NOT modify files tracked in the repository.",
    ]
    if pitfalls:
        parts += ["", "Known pitfalls for this language:"] + [f"- {p}" for p in pitfalls]
    if previous is not None:
        parts += [
            "",
            "The previous script did not work. Previous script:",
            "```bash",
            previous.rstrip(),
            "```",
            "",
            "What happened:",
            (failure or "")[-4000:],
            "",
            "Return a corrected script.",
        ]
    parts += ["", "Return only the script, in a single ```bash fenced block."]
    return "\n".join(parts)


def _capture_script(test_cmds: list[str]) -> str:
    """Run the suite with xtrace off, matching gate 1's capture shape exactly.

    The subshell's stderr is redirected into the text we parse, so with xtrace
    on `+ cmd` lines land in it — which the jest parser pushes onto the
    describe stack, prefixing every subsequent test id.
    """
    return f"set +x\ncd /workspace\n( {' && '.join(test_cmds)} ) 2>&1\n"


def distill_setup_recipe(
    *,
    bootstrap,
    test_cmds: list[str],
    base_image: str,
    language,
    llm_spec,
    options,
    sandbox: RecipeSandbox,
    debug_dir: Path | None = None,
) -> RecipeOutcome:
    """Distil a setup.sh from the bootstrap transcript, then prove it.

    The sandbox must already be running a container built from the EXACT
    environment/Dockerfile we are about to emit — not a lookalike. That is
    what makes `harbor run -a oracle == 1.0` true by construction rather than
    by hope, and it is why the caller owns the sandbox lifecycle.
    """
    source = recipe_source_from_bootstrap(bootstrap)
    if source is None:
        return RecipeOutcome(None, "", 0, 0.0, 0.0, 0.0, "no_recipe_source", [])

    preset = PRESETS.get(language)
    pitfalls = tuple(getattr(preset, "known_pitfalls", ()) or ())

    history: list[RecipeAttempt] = []
    cost = 0.0
    previous: str | None = None
    failure: str | None = None

    for attempt in range(1, options.max_recipe_attempts + 1):
        response = complete(
            llm_spec,
            system=_SYSTEM,
            user=_prompt(
                source=source,
                test_cmds=test_cmds,
                base_image=base_image,
                pitfalls=pitfalls,
                previous=previous,
                failure=failure,
            ),
            max_tokens=options.max_llm_tokens,
            temperature=options.llm_temperature,
        )
        cost += response.cost_usd
        script = extract_script(response.content)
        if script is None:
            previous, failure = response.content[:2000], "Response was not a shell script."
            history.append(RecipeAttempt("", 1, failure, False))
            continue

        setup = sandbox.exec(
            f"cd /workspace && cat > /workspace/setup.sh <<'R2E_EOF'\n{script}\nR2E_EOF\n"
            "bash /workspace/setup.sh",
            timeout=options.recipe_verify_timeout_sec,
        )
        setup_time = setup.duration_sec
        run = sandbox.exec(_capture_script(test_cmds), timeout=options.recipe_verify_timeout_sec)
        log = run.stdout + (("\n" + run.stderr) if run.stderr.strip() else "")
        # The trailing `--` keeps untracked files out of the verdict: an
        # untracked-only tree exits 0, which is what a correct recipe leaves.
        diff = sandbox.exec("git -C /workspace diff --quiet HEAD --", timeout=120)
        dirty = diff.exit_code != 0
        history.append(RecipeAttempt(script, run.exit_code, log, dirty))

        if run.exit_code == 0 and not dirty:
            return RecipeOutcome(
                script, log, attempt, cost, setup_time, run.duration_sec, None, history
            )

        previous = script
        if dirty:
            failure = f"{_TRACKED_FILE_COMPLAINT}\n\nChanged files:\n{diff.stdout[-2000:]}"
        else:
            combined = (setup.truncated(2000) + "\n" + log) if setup.exit_code else log
            failure = combined[-4000:]

    # Derived from the LAST recorded attempt, not a carried boolean: a run
    # that goes dirty on attempt N but ends on an unparseable attempt N+1
    # must be labelled recipe_unverified, not recipe_edits_tracked_files —
    # the actual last attempt told us nothing about tracked-file dirtiness.
    # `history` can be empty if max_recipe_attempts <= 0 (no validator floors
    # it), in which case the loop body never runs at all — `and` short-
    # circuits before indexing so this stays total rather than IndexError-ing.
    reason = (
        "recipe_edits_tracked_files"
        if history and history[-1].tracked_dirty
        else "recipe_unverified"
    )
    if debug_dir is not None:
        _dump_attempts(debug_dir, history)
    return RecipeOutcome(None, "", len(history), cost, 0.0, 0.0, reason, history)


def _dump_attempts(debug_dir: Path, history: list[RecipeAttempt]) -> None:
    """The .debug_skips/<task_id>/ convention equivalence_tests set in v0.8.7."""
    debug_dir.mkdir(parents=True, exist_ok=True)
    for i, a in enumerate(history, start=1):
        (debug_dir / f"attempt_{i}.sh").write_text(a.script, encoding="utf-8")
        (debug_dir / f"attempt_{i}.log").write_text(
            f"exit_code={a.exit_code} tracked_dirty={a.tracked_dirty}\n\n{a.log}",
            encoding="utf-8",
        )


def _run(args: list[str], *, timeout: int) -> ExecResult:
    """`subprocess.run` that degrades a timeout to an ExecResult instead of
    raising. Mirrors `bootstrap.docker._run`'s TimeoutExpired handling exactly
    (exit_code=124, stderr names the timeout) — this module keeps its own
    copy rather than importing that function, which is private to its module.

    Every `EnvSetupSandbox` subprocess call goes through this (or
    `_run_ignore` below for calls whose result was already discarded), so a
    hung `docker exec`/`docker build`/`docker run` degrades to a failed
    ExecResult that the retry loop in `distill_setup_recipe` can act on,
    rather than an uncaught exception that crashes the whole `env_setup` run
    (and, via `close()`, would also skip container/image cleanup).
    """
    start = time.monotonic()
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout
        if isinstance(stdout, (bytes, bytearray)):
            stdout = stdout.decode(errors="replace")
        return ExecResult(
            exit_code=124,
            stdout=stdout or "",
            stderr=f"[timeout after {timeout}s]",
            duration_sec=time.monotonic() - start,
        )
    return ExecResult(proc.returncode, proc.stdout, proc.stderr, time.monotonic() - start)


def _run_ignore(args: list[str], *, timeout: int) -> None:
    """Fire-and-forget subprocess call whose result every caller already
    discards (mkdir/cp staging, container/image teardown) — same tolerance
    for a timeout as for any other failure: swallow it, don't raise.
    """
    try:
        subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        pass


class EnvSetupSandbox:
    """Builds a Dockerfile string and holds a live container to exec into.

    `DockerSandbox` docker-cps a host repo dir into the container; here the
    image already carries /workspace from its own clone, so a cp would
    clobber it. Hence a separate, smaller primitive.
    """

    def __init__(self, container_id: str, tag: str) -> None:
        self.container_id = container_id
        self.tag = tag

    @classmethod
    def build_and_start(
        cls,
        dockerfile: str,
        *,
        tag: str,
        platform: str = "linux/amd64",
        build_timeout: int = 1800,
    ) -> EnvSetupSandbox:
        with tempfile.TemporaryDirectory(prefix="r2e-envsetup-build-") as tmp:
            ctx = Path(tmp)
            (ctx / "Dockerfile").write_text(dockerfile, encoding="utf-8")
            build = _run(
                ["docker", "build", "--platform", platform, "-t", tag, str(ctx)],
                timeout=build_timeout,
            )
        if build.exit_code != 0:
            raise DockerError(f"docker build failed: {build.stderr.strip()[:400]}")
        run = _run(
            [
                "docker",
                "run",
                "-d",
                "--platform",
                platform,
                "-w",
                "/workspace",
                tag,
                "sleep",
                "infinity",
            ],
            timeout=300,
        )
        if run.exit_code != 0:
            raise DockerError(f"docker run failed: {run.stderr.strip()[:400]}")
        return cls(run.stdout.strip(), tag)

    def exec(self, script: str, *, timeout: int) -> ExecResult:
        return _run(
            ["docker", "exec", "-i", self.container_id, "bash", "-lc", script], timeout=timeout
        )

    def put_files(self, files: Mapping[str, str], dest_dir: str) -> None:
        """Copy emitted artifacts in. Used by F' to stage the task's tests/."""
        with tempfile.TemporaryDirectory(prefix="r2e-envsetup-put-") as tmp:
            staged = Path(tmp)
            for rel, body in files.items():
                target = staged / Path(rel).name
                target.write_text(body, encoding="utf-8")
            _run_ignore(["docker", "exec", self.container_id, "mkdir", "-p", dest_dir], timeout=60)
            for path in staged.iterdir():
                _run_ignore(
                    ["docker", "cp", str(path), f"{self.container_id}:{dest_dir}/{path.name}"],
                    timeout=120,
                )

    def close(self) -> None:
        for args in (["rm", "-f", self.container_id], ["image", "rm", "-f", self.tag]):
            _run_ignore(["docker", *args], timeout=120)
