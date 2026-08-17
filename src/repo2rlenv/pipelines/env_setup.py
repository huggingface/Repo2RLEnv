"""Repo2Run/SetupBench-style: hand an agent a bare repo and grade it on making
the repo's own test suite install and run green.

Bootstrap still runs — it's the cheapest way to get a working recipe out of
an agent — but its IMAGE is thrown away. What survives is the transcript (raw
material for the gold recipe), `test_cmds` (what "the suite" means here), and
`language`. The emitted task's `FROM` is the bare base image bootstrap started
from, so the gold recipe distilled from the transcript replays against
identical ground: same base image, same starting commit, nothing pre-baked.

Flow, end to end: bootstrap runs (image discarded) -> the recipe is distilled
from its transcript and proven in a container built from the EXACT Dockerfile
this module emits -> the green run's parsed test list becomes the FAIL_TO_PASS
set -> the emitted `tests/test.sh` is dry-run against that same known-good
container (F') before the task ships. There is no other route to the F2P set
and no other proof that the emitted gates can pass at all.

----------------------------------------------------------------------------
Acknowledgment
----------------------------------------------------------------------------
The "agent-bootstraps-an-environment, then a gold recipe is distilled and
verified from scratch, graded against the repo's OWN test suite from a bare
starting point" shape this pipeline implements is informed by:

  Repo2Run (ByteDance, arXiv:2502.13681)
  https://github.com/bytedance/Repo2Run    (Apache-2.0)

  SetupBench (Microsoft, arXiv:2507.09063)
  https://github.com/microsoft/SetupBench    (MIT)

  EnvBench (JetBrains Research, ICLR '25 DL4Code, arXiv:2503.14443)
  https://github.com/JetBrains-Research/EnvBench    (MIT)

  PEP 610 — Recording the Direct URL Origin of Installed Distributions
  https://peps.python.org/pep-0610/

This module is an INDEPENDENT IMPLEMENTATION — no code is copied from any of
the three prior-art repos. It reuses only the general shape (agent bootstraps
a working environment; a clean recipe is distilled and independently
re-verified from a bare state; the provenance-probe design borrows PEP 610's
non-forgeable signal) and reimplements it from scratch against this repo's
own LLM/Docker primitives plus Python stdlib. None of the upstream licenses
apply to this file; Repo2RLEnv is Apache-2.0.
----------------------------------------------------------------------------
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from repo2rlenv.bootstrap.docker import DockerError
from repo2rlenv.bootstrap.runner import BootstrapError, ensure_bootstrap
from repo2rlenv.bootstrap.spec import LanguageHint
from repo2rlenv.emitter.harbor import HarborTask, write_harbor_task
from repo2rlenv.pipelines._env_setup_artifacts import (
    ORACLE_SOLVE_SCRIPT,
    build_env_setup_aux_files,
    build_env_setup_dockerfile,
    build_env_setup_instruction,
    build_recipe_patch,
)
from repo2rlenv.pipelines._env_setup_lang import (
    PROBE_NONE,
    bare_base_image,
    probe_kind_for,
    resolve_package_names,
)
from repo2rlenv.pipelines._eval_script import normalize_test_cmds_for_runtime
from repo2rlenv.pipelines._pr_runtime_verifier import (
    PASSED,
)
from repo2rlenv.pipelines._pr_runtime_verifier import (
    detect_runner as detect_verifier_runner,
)
from repo2rlenv.pipelines._pr_runtime_verifier import (
    parse_logs as parse_verifier_logs,
)
from repo2rlenv.pipelines._setup_recipe import EnvSetupSandbox, distill_setup_recipe
from repo2rlenv.pipelines.base import PipelineResult
from repo2rlenv.sources import Capability, capabilities_for
from repo2rlenv.spec.input import GenerationInput, PipelineName
from repo2rlenv.spec.options import EnvSetupOptions

logger = logging.getLogger(__name__)

# Module-level so tests can monkeypatch it without touching Docker.
_sandbox_factory = EnvSetupSandbox.build_and_start


class EnvSetupPipeline:
    """Repo2Run/SetupBench-style: the agent makes a bare repo's suite run green.

    Bootstrap still runs, but its IMAGE is thrown away. What we keep is the
    transcript (raw material for the gold recipe), test_cmds (what "the suite"
    means here), and language. The emitted task's FROM is the bare base image
    bootstrap started from, so the gold recipe replays against identical
    ground.
    """

    name: ClassVar[PipelineName] = PipelineName.ENV_SETUP
    requires_bootstrap: ClassVar[bool] = True
    experimental: ClassVar[bool] = True
    supported_languages: ClassVar[frozenset[LanguageHint] | None] = None
    required_capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.REMOTE_CLONE})

    def __init__(self, input: GenerationInput, options: BaseModel, bootstrap: Any = None) -> None:
        if Capability.REMOTE_CLONE not in capabilities_for(input.repo.source_kind):
            raise ValueError(
                f"pipeline 'env_setup' requires Capability.REMOTE_CLONE, which "
                f"source kind '{input.repo.source_kind}' does not provide: the "
                f"emitted Dockerfile must be able to clone the repo, and a "
                f"local path is not reachable from a `docker build`."
            )
        if input.llm is None:
            raise ValueError("env_setup requires --llm: recipe distillation is an LLM call")
        self.input = input
        self.options: EnvSetupOptions = options  # type: ignore[assignment]
        self.bootstrap = bootstrap
        self._progress_cb = None

    def set_progress_callback(self, cb) -> None:
        self._progress_cb = cb

    def _emit_progress(self, name: str, outcome: str, reason: str = "") -> None:
        if self._progress_cb is not None:
            try:
                self._progress_cb(name=name, outcome=outcome, reason=reason)
            except Exception as exc:
                logger.debug("progress callback failed: %s", exc)

    # ---- Run loop ---------------------------------------------------------

    def run(self, out_dir: Path) -> PipelineResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        refs = self._candidate_refs()
        skip_reasons: dict[str, int] = {}
        emitted = 0
        seen_shas: set[str] = set()
        total_cost = 0.0

        def skip(reason: str, ref: str) -> None:
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            self._emit_progress(ref, "skipped", reason)
            logger.info("env_setup: skipping ref %s — %s", ref, reason)

        for ref in refs:
            try:
                bootstrap = self._bootstrap_for_ref(ref)
            except (BootstrapError, DockerError) as exc:
                # DockerError can propagate uncaught from DockerSandbox.start /
                # commit — runner.py wraps neither and cmd_generate catches only
                # BootstrapError, so today it surfaces as a traceback.
                logger.warning("bootstrap failed for %s: %s", ref, exc)
                skip("bootstrap_failed", ref)
                continue

            reason = self._bootstrap_quality_filter(bootstrap)
            if reason:
                skip(reason, ref)
                continue

            sha = bootstrap.ref
            if sha in seen_shas:
                continue
            seen_shas.add(sha)

            try:
                outcome = self._emit_one(out_dir, ref, sha, bootstrap)
            except Exception as exc:  # catch-all, never halt the whole run for one bad ref
                logger.warning("env_setup build_failed for %s: %s", ref, exc, exc_info=True)
                skip("build_failed", ref)
                continue

            if isinstance(outcome, str):
                skip(outcome, ref)
                continue
            emitted += 1
            total_cost += outcome
            self._emit_progress(ref, "emitted")

        logger.info("env_setup: bootstrap+distillation spend $%.4f", total_cost)
        return PipelineResult(
            candidates=len(refs),
            emitted=emitted,
            skipped=sum(skip_reasons.values()),
            out_dir=out_dir,
            skip_reasons=skip_reasons,
        )

    # ---- Discovery / filters (domain-named, never a generic _should_skip) --

    def _candidate_refs(self) -> list[str]:
        """repo.ref is always first and always a candidate.

        Anything else wastes the bootstrap cmd_generate already paid for — the
        most expensive single step here. Dedup on the RESOLVED SHA happens in
        run(), not here: RepoSpec.ref defaults to "HEAD", so refs=["<sha-of-HEAD>"]
        would otherwise emit two directories with the same id.
        """
        primary = self.input.repo.ref
        extra = [r for r in (self.options.refs or []) if r != primary]
        return [primary, *extra][: self.options.limit]

    def _bootstrap_for_ref(self, ref: str):
        """The primary ref reuses the bootstrap cmd_generate already paid for;
        every other ref re-bootstraps on the SAME (write-back-overridden)
        BootstrapSpec so extra refs build on identical ground to the primary.
        """
        if ref == self.input.repo.ref and self.bootstrap is not None:
            return self.bootstrap
        return ensure_bootstrap(
            repo=self.input.repo.model_copy(update={"ref": ref}),
            spec=self.input.bootstrap,
            llm=self.input.llm,
            auth=self.input.auth,
        )

    def _bootstrap_quality_filter(self, bootstrap) -> str | None:
        if bootstrap.extra.get("source") == "user_dockerfile":
            return "bootstrap_source_unsupported"
        if not bootstrap.smoke_passed or not bootstrap.verify_passed:
            return "bootstrap_failed"
        expected = bare_base_image(self.input.bootstrap, bootstrap.language)
        recon = bootstrap.dockerfile_reconstruction or ""
        for line in recon.splitlines():
            if line.strip().upper().startswith("FROM "):
                if line.split(None, 1)[1].strip() != expected:
                    return "base_image_mismatch"
                break
        return None

    def _target_test_filter(self, status_map: dict[str, str]) -> tuple[list[str], str | None]:
        """Floor first, THEN truncate.

        The floor asks "is this repo gradeable" (a property of the suite); the
        cap asks "how much of it do we grade" (a property of our budget).
        Truncating first would let the cap answer the floor's question: min=5
        with max=3 would skip every task in the corpus instead of grading 3
        tests from suites that pass at least 5. SKIPPED is dropped explicitly
        — an agent that installs nothing still "passes" a skipped test.
        """
        passed = sorted(t for t, s in status_map.items() if s == PASSED)
        if len(passed) < self.options.min_target_tests:
            return [], "too_few_tests"
        if self.options.max_target_tests:
            passed = passed[: self.options.max_target_tests]
        return passed, None

    # ---- Per-ref emission ---------------------------------------------------

    def _emit_one(self, out_dir: Path, ref: str, sha: str, bootstrap) -> str | float:
        """Steps B'->F' plus emission for one ref. Returns a cost, or a skip reason."""
        cmds = [c for c in normalize_test_cmds_for_runtime(bootstrap.test_cmds) if c.strip()]
        if not cmds:
            return "no_runnable_test_cmds"

        runner = detect_verifier_runner(" ".join(cmds))
        if runner == "unknown":
            return "runner_undetectable"

        base_image = bare_base_image(self.input.bootstrap, bootstrap.language)
        # RepoSpec.url is already the normalized clone URL (no .git suffix); there
        # is no `clone_url` property. authed_clone_url handles both hosts from it.
        repo_url = self.input.repo.url
        dockerfile = build_env_setup_dockerfile(
            base_image=base_image,
            repo_url=repo_url,
            base_commit=sha,
            scrub_history=self.options.scrub_git_history,
        )
        task_name = f"{self._slug()}-envsetup-{sha[:12]}"

        sandbox = _sandbox_factory(dockerfile, tag=f"r2e-envsetup/{task_name}:build")
        try:
            recipe = distill_setup_recipe(
                bootstrap=bootstrap,
                test_cmds=cmds,
                base_image=base_image,
                language=bootstrap.language,
                llm_spec=self.input.llm,
                options=self.options,
                sandbox=sandbox,
                debug_dir=out_dir / ".debug_skips" / task_name,
            )
            if recipe.skip_reason:
                return recipe.skip_reason

            status_map = parse_verifier_logs(runner, recipe.log)
            f2p, reason = self._target_test_filter(status_map)
            if reason:
                return reason

            # Both of these need the live container, so compute them before the
            # `finally` closes it.
            has_lockfile = _has_lockfile(sandbox)
            package, dist_name = _resolve_package_names_in_container(sandbox, bootstrap.language)
            probe = (
                probe_kind_for(bootstrap.language) if self.options.provenance_gate else PROBE_NONE
            )
            if package is None:
                # A missing import name leaves the probe nothing to check on
                # either rung. A missing dist_name costs nothing — the direct_url
                # OR falls through to the import check — so it does not degrade.
                probe = PROBE_NONE

            aux = self._aux(bootstrap, cmds, runner, probe, sha, package, dist_name, f2p)
            if self.options.provenance_gate and probe != PROBE_NONE:
                reward, status = _dry_run_gates(sandbox=sandbox, aux=aux)
                if reward != 1.0:
                    if status not in ("provenance_unreadable", "package_not_from_source"):
                        return "gates_unverified"
                    # One step to `none`; there is no intermediate rung. `path`'s
                    # check is a strict subset of `direct_url`'s and can never
                    # pass where direct_url failed.
                    probe = PROBE_NONE
                    aux = self._aux(bootstrap, cmds, runner, probe, sha, package, dist_name, f2p)
                    reward, status = _dry_run_gates(sandbox=sandbox, aux=aux)
                    if reward != 1.0:
                        return "gates_unverified"
            else:
                reward, status = _dry_run_gates(sandbox=sandbox, aux=aux)
                if reward != 1.0:
                    return "gates_unverified"
        finally:
            sandbox.close()

        task = self._build_task(
            name=task_name,
            ref=ref,
            sha=sha,
            bootstrap=bootstrap,
            cmds=cmds,
            runner=runner,
            base_image=base_image,
            dockerfile=dockerfile,
            aux=aux,
            f2p=f2p,
            probe=probe,
            recipe=recipe,
            has_lockfile=has_lockfile,
        )
        # write_harbor_task materializes task_dir; _run_oracle_gate below can
        # raise from OUTSIDE its own try/except (e.g. shutil.copytree onto a
        # full disk) — a raise anywhere in this region must not leave an
        # unverified task directory on disk. `out_dir / task_name` (not a
        # `task_dir` local) is used in the handler so it's correct even if
        # `write_harbor_task` itself raises before returning.
        try:
            task_dir = write_harbor_task(task, out_dir)

            if self.options.emit_solution and self.options.oracle_gate:
                gate = self._run_oracle_gate(task_dir, self.options.effective_oracle_timeout_sec)
                if gate is not None and gate != 1.0:
                    shutil.rmtree(task_dir, ignore_errors=True)
                    return "oracle_gate_failed"
        except Exception:
            shutil.rmtree(out_dir / task_name, ignore_errors=True)
            raise

        return recipe.cost_usd + bootstrap.llm_cost_estimate_usd

    # ---- Helpers ------------------------------------------------------------

    def _slug(self) -> str:
        """`{owner}__{name}`, matching commit_runtime's convention.

        The task id appends the RESOLVED SHA, not the ref string: `HEAD` is not
        a stable id, and two runs a week apart would collide on one directory
        name while describing different trees.
        """
        owner, name = self.input.repo.owner_name
        return f"{owner}__{name}"

    def _aux(self, bootstrap, cmds, runner, probe, sha, package, dist_name, f2p) -> dict[str, str]:
        """Rebuilt rather than mutated when the probe degrades — provenance.json,
        test.sh, and the shipped probe file must stay consistent with each other.
        """
        return build_env_setup_aux_files(
            language=str(bootstrap.language),
            test_cmds=cmds,
            runner=runner,
            probe=probe,
            base_commit=sha,
            package=package,
            dist_name=dist_name,
            f2p=f2p,
            p2p=[],  # P2P is empty: there is no pre-existing passing set to
            # protect. grade() maps empty P2P to p2p_rate = 1.0, so
            # reward = f2p_rate falls out with no verifier change.
        )

    def _build_task(
        self,
        *,
        name,
        ref,
        sha,
        bootstrap,
        cmds,
        runner,
        base_image,
        dockerfile,
        aux,
        f2p,
        probe,
        recipe,
        has_lockfile,
    ) -> HarborTask:
        owner, repo_name = self.input.repo.owner_name
        test_cmd = " && ".join(cmds)
        emit_solution = self.options.emit_solution
        return HarborTask(
            name=name,
            org=owner,
            description=f"Make {owner}/{repo_name}'s test suite install and run from a bare image",
            instruction=build_env_setup_instruction(
                repo_slug=f"{owner}/{repo_name}",
                ref=ref,
                base_commit=sha,
                max_setup_time_sec=self.options.max_setup_time_sec,
            ),
            repo2env={
                "pipeline": "env_setup",
                "repo": f"{owner}/{repo_name}",
                "base_commit": sha,
                "reward_kinds": ["test_execution"],
                "env_setup": {
                    "language": str(bootstrap.language),
                    "base_image": base_image,
                    "test_cmd": test_cmd,
                    "runner": runner,
                    "target_test_count": len(f2p),
                    "recipe_lines": sum(
                        1
                        for ln in (recipe.setup_sh or "").splitlines()
                        if ln.strip() and not ln.lstrip().startswith("#")
                    ),
                    "recipe_attempts": recipe.attempts,
                    "oracle_setup_time_sec": round(recipe.setup_time_sec, 3),
                    "oracle_test_time_sec": round(recipe.test_time_sec, 3),
                    "agent_time_budget_sec": self.options.max_setup_time_sec,
                    "base_commit": sha,
                    "has_lockfile": has_lockfile,
                    "bootstrap_cost_usd": round(
                        bootstrap.llm_cost_estimate_usd + recipe.cost_usd, 6
                    ),
                    "provenance_gate": self.options.provenance_gate,
                    "provenance_probe": probe,
                    "reward_granularity": "graded",
                    "oracle": "recipe" if emit_solution else "none",
                },
            },
            difficulty="medium",
            category="environment-setup",
            keywords=["env_setup", str(bootstrap.language), runner],
            oracle_diff=build_recipe_patch(recipe.setup_sh) if emit_solution else None,
            environment_dockerfile=dockerfile,
            solve_script=ORACLE_SOLVE_SCRIPT if emit_solution else None,
            agent_timeout_sec=float(self.options.max_setup_time_sec),
            verifier_timeout_sec=float(self.options.verifier_timeout_sec),
            aux_files=aux,
        )

    def _run_oracle_gate(self, task_dir: Path, timeout_sec: int) -> float | None:
        """Run `harbor run -a oracle` on the emitted task and return the reward.

        Returns None if the harbor CLI isn't on PATH or exits abnormally — the
        caller treats this as a soft-skip (env is kept). Returns a float in
        [0.0, 1.0] on a real run. Copied from `pr_to_env._run_oracle_gate`
        verbatim (same `subprocess.run(timeout=...)` shape around
        `harbor run -a oracle`).
        """
        harbor = shutil.which("harbor")
        if harbor is None:
            return None
        # `harbor run` takes `-p <dataset dir>` and runs every task under it —
        # there is no --task-dir flag. Isolate this one task in a scratch
        # dataset so the gate grades exactly the env we just emitted.
        with tempfile.TemporaryDirectory(prefix="r2e-oracle-gate-") as tmp:
            scratch = Path(tmp)
            dataset = scratch / "dataset"
            dataset.mkdir()
            shutil.copytree(task_dir, dataset / task_dir.name)
            jobs = scratch / "jobs"
            try:
                proc = subprocess.run(
                    [
                        harbor,
                        "run",
                        "-p",
                        str(dataset),
                        "-a",
                        "oracle",
                        "--env",
                        "docker",
                        "-n",
                        "1",
                        "-y",
                        "--quiet",
                        "--jobs-dir",
                        str(jobs),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=timeout_sec,
                    check=False,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                logger.warning("oracle-gate subprocess failed for %s: %s", task_dir.name, exc)
                return None
            # Primary signal: the reward.txt harbor's verifier writes per run.
            for reward_file in sorted(jobs.glob("*/*/verifier/reward.txt")):
                try:
                    return float(reward_file.read_text().strip())
                except (OSError, ValueError):
                    continue
            # Fallback: scrape `reward=<float>` out of the CLI output.
            m = re.search(r"reward\s*[=:]\s*([0-9.]+)", f"{proc.stdout}\n{proc.stderr}")
            if m:
                try:
                    return float(m.group(1))
                except ValueError:
                    return None
            logger.warning(
                "oracle-gate produced no reward for %s (harbor exit=%d): %s",
                task_dir.name,
                proc.returncode,
                (proc.stderr or proc.stdout)[-400:].strip(),
            )
            return None


def _dry_run_gates(*, sandbox, aux: dict[str, str]) -> tuple[float, str]:
    """F' — dry-run the WHOLE emitted test.sh in the green container.

    Unconditional, and it runs even when oracle_gate=False and even when
    emit_solution=False: it is not a shipping gate, it is how the probe gets
    chosen and how the script gets proven. A gate that cannot pass on a
    known-good environment is a gate that would zero every agent.

    It runs test.sh WITHOUT re-running setup.sh, so it does catch a recipe
    that depends on an in-tree edit: gate 1/2 reverts the edit, gate 1 goes
    red, reward != 1.0.
    """
    sandbox.put_files(aux, "/tests")
    sandbox.exec("chmod +x /tests/*.sh", timeout=60)
    sandbox.exec("bash /tests/test.sh", timeout=3600)
    details = sandbox.exec("cat /logs/verifier/reward-details.json", timeout=60)
    try:
        parsed = json.loads(details.stdout)
        return float(parsed.get("reward", 0.0)), str(parsed.get("parse_status", ""))
    except (ValueError, TypeError):
        return 0.0, "verifier_crashed"


_LOCKFILES = ("uv.lock", "poetry.lock", "package-lock.json", "Cargo.lock", "go.sum")


def _has_lockfile(sandbox) -> bool:
    """One `git ls-tree` against the container's checkout.

    A repo with a lockfile is substantially easier (`uv pip sync uv.lock` is
    nearly the whole task), so training regimes need to rebalance. It costs
    one command, so we record it rather than debate it.
    """
    res = sandbox.exec("git -C /workspace ls-tree --name-only HEAD", timeout=120)
    tracked = set(res.stdout.split())
    return any(name in tracked for name in _LOCKFILES)


_MANIFESTS = ("pyproject.toml", "setup.cfg", "package.json")


def _resolve_package_names_in_container(sandbox, language) -> tuple[str | None, str | None]:
    """Copy the manifests out of the container, then reuse the shared resolver.

    `resolve_package_names` takes a host `Path`, and our checkout only exists
    inside the container, so materialize just the three manifest files into a
    temp dir. Both names are baked at emit time and never recovered at run
    time: `importlib.metadata.packages_distributions()` misses .pth-style
    editable installs.

    The two fail differently, and the asymmetry is the spec: a missing
    dist_name costs nothing on the direct_url probe (the OR falls through to
    the import check), while a missing `package` leaves the probe nothing to
    check on either rung, so the caller ships probe="none".
    """
    with tempfile.TemporaryDirectory(prefix="r2e-envsetup-names-") as tmp:
        root = Path(tmp)
        for name in _MANIFESTS:
            res = sandbox.exec(f"cat /workspace/{name} 2>/dev/null", timeout=60)
            if res.exit_code == 0 and res.stdout.strip():
                (root / name).write_text(res.stdout, encoding="utf-8")
        return resolve_package_names(root, language)
