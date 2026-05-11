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
from datetime import datetime, timezone
from pathlib import Path

from repo2rlenv.auth import auth_clone_url, resolve_github_token
from repo2rlenv.bootstrap import cache as cache_mod
from repo2rlenv.bootstrap.agent import run_agent_loop
from repo2rlenv.bootstrap.docker import (
    DockerError,
    DockerSandbox,
    _run,
    is_docker_available,
)
from repo2rlenv.bootstrap.language import base_image_for, detect_language
from repo2rlenv.bootstrap.spec import BootstrapResult, LanguageHint
from repo2rlenv.spec.input import AuthSpec, BootstrapSpec, LLMSpec, RepoSpec

logger = logging.getLogger(__name__)


class BootstrapError(RuntimeError):
    """Raised when bootstrap cannot complete (after honoring `enabled=False`)."""


def _resolve_head_sha(local_clone: Path) -> str:
    """Return the full 40-char SHA at HEAD of the local clone."""
    r = subprocess.run(
        ["git", "-C", str(local_clone), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False, timeout=30,
    )
    if r.returncode != 0:
        raise BootstrapError(f"git rev-parse HEAD failed: {r.stderr.strip()}")
    return r.stdout.strip()


def _scrub_token(text: str, token: str | None) -> str:
    return text.replace(token, "***") if token else text


def _shallow_clone_at_ref(
    repo_url: str, ref: str, token: str | None, dest: Path, *, depth: int = 1
) -> None:
    """Clone repo and check out `ref`. Works for HEAD, branch, tag, or commit SHA.

    Strategy:
      1. ref="HEAD" → plain shallow clone of default branch.
      2. ref looks branch/tag-like → try `git clone --branch <ref>`. Works for
         any ref ending up as a tag or branch on the remote.
      3. Otherwise (commit SHA / fallback) → bare clone + `git fetch origin <ref>`
         + `git checkout`.
    """
    url = auth_clone_url(repo_url, token)

    if ref in ("", "HEAD"):
        r = subprocess.run(
            ["git", "clone", "--depth", str(depth), url, str(dest)],
            capture_output=True, text=True, timeout=300, check=False,
        )
        if r.returncode != 0:
            raise BootstrapError(
                f"git clone failed: {_scrub_token(r.stderr, token).strip()[:400]}"
            )
        return

    # Try clone --branch first; works for branches and tags
    r = subprocess.run(
        ["git", "clone", "--depth", str(depth), "--branch", ref, url, str(dest)],
        capture_output=True, text=True, timeout=300, check=False,
    )
    if r.returncode == 0:
        return

    # Fallback: clone default, then fetch + checkout the ref (handles SHAs)
    logger.info("clone --branch %r failed, falling back to fetch-by-ref", ref)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    r = subprocess.run(
        ["git", "clone", "--filter=blob:none", "--no-checkout", url, str(dest)],
        capture_output=True, text=True, timeout=300, check=False,
    )
    if r.returncode != 0:
        raise BootstrapError(
            f"git clone (fallback) failed: {_scrub_token(r.stderr, token).strip()[:400]}"
        )
    r = subprocess.run(
        ["git", "-C", str(dest), "fetch", "--depth", str(depth), "origin", ref],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if r.returncode != 0:
        raise BootstrapError(
            f"git fetch origin {ref!r} failed (is this a valid branch/tag/commit?): "
            f"{_scrub_token(r.stderr, token).strip()[:400]}"
        )
    r = subprocess.run(
        ["git", "-C", str(dest), "checkout", ref],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if r.returncode != 0:
        raise BootstrapError(
            f"git checkout {ref!r} failed: {_scrub_token(r.stderr, token).strip()[:400]}"
        )


def _resolve_repo_digest(tag: str) -> str | None:
    """Return the pullable `repo@sha256:...` digest for a tagged image, if any.

    `docker image inspect` exposes registry-qualified digests in `RepoDigests`
    only AFTER a `docker push`. Used to upgrade `image_digest` from a local Id
    to a real registry digest once an image has been pushed.
    """
    r = _run(["docker", "image", "inspect", tag, "--format", "{{json .RepoDigests}}"], timeout=15)
    if not r.ok:
        return None
    try:
        digests = json.loads(r.stdout.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(digests, list) and digests:
        return digests[0]
    return None


def _bootstrap_from_user_dockerfile(
    repo: RepoSpec,
    spec: BootstrapSpec,
    llm: LLMSpec,
    token: str | None,
    *,
    force: bool,
) -> BootstrapResult:
    """Bypass the agent loop: build the user-supplied Dockerfile directly.

    Skips LLM iteration entirely. The Dockerfile is responsible for installing
    everything; we just clone the repo and run `docker build` with the repo
    as the build context. Cached identically to the agent-driven path.
    """
    dockerfile = spec.user_dockerfile
    assert dockerfile is not None  # caller checks
    if not dockerfile.is_file():
        raise BootstrapError(f"user_dockerfile not found: {dockerfile}")

    with tempfile.TemporaryDirectory(prefix="r2e-clone-") as tmp:
        clone_dir = Path(tmp) / "repo"
        logger.info("cloning %s @ %s for user_dockerfile build", repo.url, repo.ref)
        _shallow_clone_at_ref(repo.url, repo.ref, token, clone_dir)
        ref_sha = _resolve_head_sha(clone_dir)
        owner_name = "/".join(repo.owner_name)

        if not force:
            cached = cache_mod.load(owner_name, ref_sha, spec.cache_dir)
            if cached is not None and cached.image_digest:
                logger.info("user_dockerfile cache hit: %s", cached.image_digest)
                return cached

        # Copy the Dockerfile into the build context so its `COPY .` (etc.) just works
        shutil.copy(dockerfile, clone_dir / "Dockerfile")
        owner, name = repo.owner_name
        slug = f"{owner}__{name}".replace("/", "__").lower()
        tag_base = spec.image_registry or "local/r2e-bootstrap"
        tag = f"{tag_base}/{slug}:{ref_sha[:12]}".lstrip("/")

        start = time.monotonic()
        r = _run(
            [
                "docker", "build",
                "--platform", spec.platform,
                "-t", tag,
                str(clone_dir),
            ],
            timeout=spec.max_seconds,
        )
        if not r.ok:
            raise BootstrapError(
                f"docker build (user_dockerfile) failed: {r.stderr.strip()[:400]}"
            )

        # Inspect for the local Id; push + re-resolve if a registry was set
        local_digest_inspect = _run(
            ["docker", "image", "inspect", tag, "--format", "{{.Id}}"], timeout=10,
        )
        image_digest = (
            local_digest_inspect.stdout.strip() if local_digest_inspect.ok else tag
        )
        pushed = False
        if spec.image_registry and "/" in spec.image_registry:
            push = _run(["docker", "push", tag], timeout=900)
            pushed = push.ok
            if pushed:
                resolved = _resolve_repo_digest(tag)
                if resolved:
                    image_digest = resolved

        result = BootstrapResult(
            image_digest=image_digest,
            image_tag=tag,
            language=LanguageHint.UNKNOWN,  # we didn't detect — the user owns the image
            repo=owner_name,
            ref=ref_sha,
            rebuild_cmds=[],     # caller supplied a Dockerfile; rebuild is up to them
            test_cmds=[],
            smoke_passed=True,   # no agent ran a smoke; trust the user
            iterations=0,
            build_time_sec=round(time.monotonic() - start, 2),
            llm_provider=llm.qualified_name,
            llm_cost_estimate_usd=0.0,
            dockerfile_reconstruction=dockerfile.read_text(encoding="utf-8"),
            pushed_to_registry=pushed,
            extra={"source": "user_dockerfile", "dockerfile_path": str(dockerfile)},
        )
        cache_mod.save(result, spec.cache_dir)
        logger.info("user_dockerfile bootstrap done: %s -> %s", owner_name, image_digest[:48])
        return result


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

    # Honor user_dockerfile override — skip agent loop entirely
    if spec.user_dockerfile is not None:
        return _bootstrap_from_user_dockerfile(repo, spec, llm, token, force=force)

    with tempfile.TemporaryDirectory(prefix="r2e-clone-") as tmp:
        clone_dir = Path(tmp) / "repo"
        logger.info("cloning %s @ %s into %s", repo.url, repo.ref, clone_dir)
        _shallow_clone_at_ref(repo.url, repo.ref, token, clone_dir)
        ref_sha = _resolve_head_sha(clone_dir)

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

            # Soft smoke gate — runs ALL test_cmds JOINED in one shell so PATH
            # exports etc. carry over. Treats individual test failures as fine
            # (pytest exit 1 = tests failed but ran; 5 = no tests collected
            # but pytest ran). We only flag as failed for env-level errors.
            # The agent's SAVE_SETUP call is the real success signal.
            smoke_ok = True
            if outcome.test_cmds:
                smoke_script = " && ".join(outcome.test_cmds)
                r = sandbox.exec(smoke_script, timeout=300)
                if r.exit_code not in (0, 1, 5):
                    smoke_ok = False
                    logger.warning(
                        "smoke test exited %d: %s (env issue, not just test failures)",
                        r.exit_code, smoke_script[:200],
                    )

            # Commit the container regardless — caller decides whether to push
            tag_base = spec.image_registry or "local/r2e-bootstrap"
            owner, name = repo.owner_name
            slug = f"{owner}__{name}".replace("/", "__").lower()
            tag = f"{tag_base}/{slug}:{ref_sha[:12]}".lstrip("/")
            image_digest = sandbox.commit(tag, message=f"r2e bootstrap {owner_name}@{ref_sha[:12]}")

            pushed = False
            if spec.image_registry and "/" in spec.image_registry:
                pushed = sandbox.push(tag)
                if pushed:
                    # After push, `docker image inspect` now returns RepoDigests
                    # like `ghcr.io/owner/foo@sha256:...` — the registry-qualified,
                    # pullable digest. Re-resolve so downstream sandboxes can pull.
                    resolved = _resolve_repo_digest(tag)
                    if resolved:
                        image_digest = resolved
                    else:
                        logger.warning(
                            "push %s succeeded but RepoDigests not populated; "
                            "image_digest stays at the local id %s",
                            tag, image_digest,
                        )

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
