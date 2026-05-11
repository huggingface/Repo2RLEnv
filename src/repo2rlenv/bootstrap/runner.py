"""Orchestrator: ensure a working Docker image exists for a (repo, ref).

This is the public API of the bootstrap module. Sandbox-required pipelines
call `ensure_bootstrap(...)` before they start synthesizing tasks.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from repo2rlenv.auth import auth_clone_url, resolve_github_token
from repo2rlenv.bootstrap import cache as cache_mod
from repo2rlenv.bootstrap.agent import run_agent_loop
from repo2rlenv.bootstrap.docker import DockerError, DockerSandbox, is_docker_available
from repo2rlenv.bootstrap.language import base_image_for, detect_language
from repo2rlenv.bootstrap.spec import BootstrapResult, LanguageHint
from repo2rlenv.spec.input import AuthSpec, BootstrapSpec, LLMSpec, RepoSpec

logger = logging.getLogger(__name__)


class BootstrapError(RuntimeError):
    """Raised when bootstrap cannot complete (after honoring `enabled=False`)."""


def _resolve_ref_to_sha(local_clone: Path, ref: str) -> str:
    """Convert HEAD / branch / tag / partial-SHA into a full 40-char SHA."""
    if ref == "HEAD":
        ref = "HEAD"
    r = subprocess.run(
        ["git", "-C", str(local_clone), "rev-parse", ref],
        capture_output=True, text=True, check=False, timeout=30,
    )
    if r.returncode != 0:
        raise BootstrapError(f"git rev-parse {ref!r} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def _shallow_clone(repo_url: str, token: str | None, dest: Path, *, depth: int = 1) -> None:
    url = auth_clone_url(repo_url, token)
    args = ["git", "clone", "--depth", str(depth), url, str(dest)]
    r = subprocess.run(args, capture_output=True, text=True, timeout=300, check=False)
    if r.returncode != 0:
        # Don't leak the token in the error message
        scrubbed = r.stderr.replace(token or "", "***") if token else r.stderr
        raise BootstrapError(f"git clone failed: {scrubbed.strip()[:400]}")


def _reconstruct_dockerfile(base_image: str, turns: list) -> str:
    """Produce a Dockerfile from the BASH commands the agent ran.

    Not always perfectly reproducible (commands may have depended on state
    from earlier non-BASH actions), but a useful starting point for users
    who want to rebuild without re-running the agent.
    """
    lines = [f"# Auto-generated from r2e-bootstrap agent transcript", f"FROM {base_image}", ""]
    for t in turns:
        if getattr(t.action, "name", None) == "BASH":
            cmd = t.action.input.replace("\n", " \\\n    ")
            lines.append(f"RUN {cmd}")
    lines.append("")
    lines.append("WORKDIR /workspace")
    return "\n".join(lines)


def ensure_bootstrap(
    repo: RepoSpec,
    spec: BootstrapSpec,
    llm: LLMSpec,
    auth: AuthSpec | None = None,
    *,
    force: bool = False,
    on_turn=None,
    on_phase=None,
) -> BootstrapResult:
    """Return a working bootstrap image for (repo, ref). Cached after first call.

    Resolution order:
      1. If `spec.user_dockerfile` is set → build it directly, no agent loop
      2. If cache hit at (repo, ref) → return cached result
      3. Else → run the agent loop in a fresh Docker sandbox

    Raises BootstrapError on failures the user should know about.
    """
    if not spec.enabled:
        raise BootstrapError("bootstrap is disabled (spec.enabled=False)")
    if not is_docker_available():
        raise BootstrapError(
            "Docker daemon is not running. Start Docker Desktop / dockerd, "
            "or run bootstrap inside a sandbox that has Docker available."
        )

    auth = auth or AuthSpec()
    token = resolve_github_token(repo, auth)
    if repo.access == "private" and not token:
        raise BootstrapError(
            "private repo requires a GitHub token. Run `gh auth login` or set GITHUB_TOKEN."
        )

    with tempfile.TemporaryDirectory(prefix="r2e-clone-") as tmp:
        clone_dir = Path(tmp) / "repo"
        logger.info("cloning %s into %s", repo.url, clone_dir)
        _shallow_clone(repo.url, token, clone_dir)
        ref_sha = _resolve_ref_to_sha(clone_dir, repo.ref)

        # Cache check (after we know the resolved SHA)
        owner_name = "/".join(repo.owner_name)
        if not force:
            cached = cache_mod.load(owner_name, ref_sha, spec.cache_dir)
            if cached is not None and cached.image_digest:
                logger.info("bootstrap cache hit: %s", cached.image_digest)
                return cached

        # Decide language + base image
        lang = LanguageHint.UNKNOWN
        if spec.languages_hint:
            try:
                lang = LanguageHint(spec.languages_hint[0])
            except ValueError:
                pass
        if lang == LanguageHint.UNKNOWN:
            lang = detect_language(clone_dir)
        base_image = spec.base_image or base_image_for(lang)

        # Spin up sandbox
        start = time.monotonic()
        with DockerSandbox.start(
            base_image,
            clone_dir,
            platform=spec.platform,
        ) as sandbox:
            # Quick sanity: git is installed in the container (most base images include it)
            outcome = run_agent_loop(
                sandbox,
                repo=owner_name,
                ref=ref_sha,
                language=lang,
                base_image=base_image,
                llm=llm,
                max_iterations=spec.max_iterations,
                max_seconds=spec.max_seconds,
                platform=spec.platform,
                on_turn=on_turn,
            )

            # Always persist the transcript — even on failure — for debugging.
            failure_slot = cache_mod.cache_key(owner_name, ref_sha, spec.cache_dir)
            failure_slot.mkdir(parents=True, exist_ok=True)
            try:
                with (failure_slot / "transcript.jsonl").open("w", encoding="utf-8") as f:
                    for turn in outcome.transcript:
                        f.write(json.dumps({
                            "step": turn.step,
                            "thought": turn.thought,
                            "action": turn.action.name,
                            "input": turn.action.input,
                            "observation": turn.observation,
                        }) + "\n")
            except OSError as exc:
                logger.warning("could not write transcript: %s", exc)

            if not outcome.success:
                raise BootstrapError(
                    f"bootstrap failed: {outcome.reason} "
                    f"(iterations={outcome.iterations}, cost≈${outcome.total_cost_estimate_usd:.2f}). "
                    f"Transcript at {failure_slot / 'transcript.jsonl'}"
                )

            # Soft smoke gate — runs test_cmds but treats individual test failures
            # as fine (pytest exit 1 = tests failed but ran; 5 = no tests collected
            # but pytest ran). We only flag as failed for env-level errors (2,3,4 etc.).
            # The agent's SAVE_SETUP call is the real success signal.
            smoke_ok = True
            for cmd in outcome.test_cmds:
                r = sandbox.exec(cmd, timeout=300)
                if r.exit_code not in (0, 1, 5):
                    smoke_ok = False
                    logger.warning(
                        "smoke test %r exited %d (env issue, not just test failures)",
                        cmd, r.exit_code,
                    )
                    break

            # Commit the container regardless — caller decides whether to push
            tag_base = spec.image_registry or "local/r2e-bootstrap"
            owner, name = repo.owner_name
            slug = f"{owner}__{name}".replace("/", "__").lower()
            tag = f"{tag_base}/{slug}:{ref_sha[:12]}".lstrip("/")
            image_digest = sandbox.commit(tag, message=f"r2e bootstrap {owner_name}@{ref_sha[:12]}")

            pushed = False
            if spec.image_registry and "/" in spec.image_registry:
                pushed = sandbox.push(tag)

        build_time = time.monotonic() - start
        dockerfile = _reconstruct_dockerfile(base_image, outcome.transcript)

        result = BootstrapResult(
            image_digest=image_digest,
            image_tag=tag,
            language=lang,
            repo=owner_name,
            ref=ref_sha,
            rebuild_cmds=outcome.rebuild_cmds,
            test_cmds=outcome.test_cmds,
            smoke_passed=smoke_ok,
            iterations=outcome.iterations,
            build_time_sec=round(build_time, 2),
            llm_provider=llm.qualified_name,
            llm_cost_estimate_usd=outcome.total_cost_estimate_usd,
            dockerfile_reconstruction=dockerfile,
            pushed_to_registry=pushed,
        )
        slot = cache_mod.save(result, spec.cache_dir)

        # Transcript was already written during the agent loop; just link it.
        transcript_path = cache_mod.cache_key(owner_name, ref_sha, spec.cache_dir) / "transcript.jsonl"
        if transcript_path.exists():
            result.transcript_path = transcript_path
            cache_mod.save(result, spec.cache_dir)  # re-save with transcript path

        logger.info(
            "bootstrap done: %s iterations=%d time=%.1fs digest=%s",
            owner_name, outcome.iterations, build_time, image_digest[:40],
        )
        return result
