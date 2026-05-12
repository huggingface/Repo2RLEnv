"""Orchestrator: ensure a working Docker image exists for a (repo, ref).

This is the public API of the bootstrap module. Sandbox-required pipelines
call `ensure_bootstrap(...)` before they start synthesizing tasks.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from repo2rlenv.auth import auth_clone_url, resolve_github_token
from repo2rlenv.bootstrap import cache as cache_mod
from repo2rlenv.bootstrap.agent import run_agent_loop
from repo2rlenv.bootstrap.docker import (
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
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if r.returncode != 0:
        raise BootstrapError(f"git rev-parse HEAD failed: {r.stderr.strip()}")
    return r.stdout.strip()


def _scrub_token(text: str, token: str | None) -> str:
    return text.replace(token, "***") if token else text


def _cache_options(spec: BootstrapSpec) -> dict[str, object]:
    """Stable, image-identity-affecting subset of the spec for the cache key.

    Anything that would produce a *different* image must go here. We
    deliberately exclude max_iterations / max_seconds / max_llm_spend_usd
    since those bound the build process, not the result.
    """
    return {
        "platform": spec.platform,
        "base_image": spec.base_image,
        "user_dockerfile": str(spec.user_dockerfile) if spec.user_dockerfile else None,
        "image_registry": spec.image_registry,
        "languages_hint": spec.languages_hint,
    }


def _run_git_streaming(
    args: list[str],
    *,
    token: str | None,
    timeout: int,
    on_progress: Callable[[str], None] | None,
) -> subprocess.CompletedProcess[str]:
    """Run git with --progress, streaming its stderr to on_progress.

    Returns a CompletedProcess-compatible result; never raises on non-zero exit.
    git emits its progress (Receiving objects, Resolving deltas...) on stderr.
    """
    if on_progress is None:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    err_buf: list[str] = []
    out_buf: list[str] = []
    last_line = ""

    def _pump_stderr() -> None:
        nonlocal last_line
        assert proc.stderr is not None
        while True:
            chunk = proc.stderr.read(256)
            if not chunk:
                break
            err_buf.append(chunk)
            for part in re.split(r"[\r\n]+", chunk):
                part = part.strip()
                if not part or part == last_line:
                    continue
                last_line = part
                try:
                    on_progress(_scrub_token(part, token)[:120])
                except Exception:
                    pass

    def _pump_stdout() -> None:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(1024)
            if not chunk:
                break
            out_buf.append(chunk)

    t_err = threading.Thread(target=_pump_stderr, daemon=True)
    t_out = threading.Thread(target=_pump_stdout, daemon=True)
    t_err.start()
    t_out.start()
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        t_err.join(timeout=2)
        t_out.join(timeout=2)
        return subprocess.CompletedProcess(args, 124, "".join(out_buf), "[timeout]")
    t_err.join(timeout=2)
    t_out.join(timeout=2)
    return subprocess.CompletedProcess(args, rc, "".join(out_buf), "".join(err_buf))


def _shallow_clone_at_ref(
    repo_url: str,
    ref: str,
    token: str | None,
    dest: Path,
    *,
    depth: int = 1,
    on_progress: Callable[[str], None] | None = None,
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
        r = _run_git_streaming(
            ["git", "clone", "--progress", "--depth", str(depth), url, str(dest)],
            token=token,
            timeout=300,
            on_progress=on_progress,
        )
        if r.returncode != 0:
            raise BootstrapError(f"git clone failed: {_scrub_token(r.stderr, token).strip()[:400]}")
        return

    # Try clone --branch first; works for branches and tags
    r = _run_git_streaming(
        ["git", "clone", "--progress", "--depth", str(depth), "--branch", ref, url, str(dest)],
        token=token,
        timeout=300,
        on_progress=on_progress,
    )
    if r.returncode == 0:
        return

    # Fallback: clone default, then fetch + checkout the ref (handles SHAs)
    logger.info("clone --branch %r failed, falling back to fetch-by-ref", ref)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    r = _run_git_streaming(
        ["git", "clone", "--progress", "--filter=blob:none", "--no-checkout", url, str(dest)],
        token=token,
        timeout=300,
        on_progress=on_progress,
    )
    if r.returncode != 0:
        raise BootstrapError(
            f"git clone (fallback) failed: {_scrub_token(r.stderr, token).strip()[:400]}"
        )
    r = _run_git_streaming(
        ["git", "-C", str(dest), "fetch", "--progress", "--depth", str(depth), "origin", ref],
        token=token,
        timeout=120,
        on_progress=on_progress,
    )
    if r.returncode != 0:
        raise BootstrapError(
            f"git fetch origin {ref!r} failed (is this a valid branch/tag/commit?): "
            f"{_scrub_token(r.stderr, token).strip()[:400]}"
        )
    r = subprocess.run(
        ["git", "-C", str(dest), "checkout", ref],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if r.returncode != 0:
        raise BootstrapError(
            f"git checkout {ref!r} failed: {_scrub_token(r.stderr, token).strip()[:400]}"
        )


def _scrub_clone_credentials(clone_dir: Path, repo_url: str) -> None:
    """Reset the clone's remote URL to a token-free form.

    `auth_clone_url()` embeds the GitHub PAT into the clone URL so the
    private fetch works, but git records that exact URL in `.git/config`.
    We later `docker cp <clone>/. <container>:/workspace` and `docker commit`,
    which bakes `.git/config` (token and all) into the published image.

    Stripping the remote URL post-clone is the simplest fix; alternatively
    we could drop `.git/` entirely, but the bootstrap agent sometimes needs
    git metadata (tags, blame) to make sense of the repo.
    """
    try:
        subprocess.run(
            ["git", "-C", str(clone_dir), "remote", "set-url", "origin", repo_url],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("could not scrub clone remote url (token may persist in image): %s", exc)


def _verify_committed_image(
    tag: str,
    test_cmds: list[str],
    *,
    platform: str,
    timeout: int = 300,
) -> tuple[bool, str]:
    """Run test_cmds in a FRESH container from the committed image.

    More authoritative than the pre-commit smoke check: confirms the layer
    diff actually captured everything the agent installed (env vars set via
    `export` in a bash session do NOT persist into the committed image).

    Returns (ok, detail). pytest exit 0/1/5 are "env works"
    (1 = tests ran but some failed, 5 = no tests collected). For non-pytest
    frameworks (cargo/go/npm/mocha) exit 1 ALSO means "tests ran but failed"
    so we accept it too — env-level errors are 2+ (cargo: command not found,
    npm ENOENT, etc.) or 127 (bin not found).
    """
    if not test_cmds:
        return (True, "(no test_cmds recorded — skipped)")

    script = " && ".join(test_cmds)
    args = [
        "docker",
        "run",
        "--rm",
        "--platform",
        platform,
        "-w",
        "/workspace",
        tag,
        "bash",
        "-lc",
        script,
    ]
    r = _run(args, timeout=timeout)
    detail = (r.stdout + r.stderr)[-200:].strip()
    # Accept exit 0, 1 (tests ran but failed — that's fine for a bootstrap),
    # 5 (pytest: no tests collected). 127 = command not found = env broken.
    # 2 = pytest internal error / collection failure = env broken.
    ok = r.exit_code in (0, 1, 5)
    return (ok, f"exit={r.exit_code}; {detail}")


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
        # Scrub token from .git/config before the dir becomes the Docker build context
        if token:
            _scrub_clone_credentials(clone_dir, repo.url)
        ref_sha = _resolve_head_sha(clone_dir)
        owner_name = "/".join(repo.owner_name)
        cache_opts = _cache_options(spec)

        if not force:
            cached = cache_mod.load(owner_name, ref_sha, spec.cache_dir, options=cache_opts)
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
                "docker",
                "build",
                "--platform",
                spec.platform,
                "-t",
                tag,
                str(clone_dir),
            ],
            timeout=spec.max_seconds,
        )
        if not r.ok:
            raise BootstrapError(f"docker build (user_dockerfile) failed: {r.stderr.strip()[:400]}")

        # Inspect for the local Id; push + re-resolve if a registry was set
        local_digest_inspect = _run(
            ["docker", "image", "inspect", tag, "--format", "{{.Id}}"],
            timeout=10,
        )
        image_digest = local_digest_inspect.stdout.strip() if local_digest_inspect.ok else tag
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
            rebuild_cmds=[],  # caller supplied a Dockerfile; rebuild is up to them
            test_cmds=[],
            smoke_passed=True,  # no agent ran a smoke; trust the user
            iterations=0,
            build_time_sec=round(time.monotonic() - start, 2),
            llm_provider=llm.qualified_name,
            llm_cost_estimate_usd=0.0,
            dockerfile_reconstruction=dockerfile.read_text(encoding="utf-8"),
            pushed_to_registry=pushed,
            extra={"source": "user_dockerfile", "dockerfile_path": str(dockerfile)},
        )
        cache_mod.save(result, spec.cache_dir, options=cache_opts)
        logger.info("user_dockerfile bootstrap done: %s -> %s", owner_name, image_digest[:48])
        return result


def _reconstruct_dockerfile(base_image: str, turns: list) -> str:
    """Produce a Dockerfile from the BASH commands the agent ran.

    Not always perfectly reproducible (commands may have depended on state
    from earlier non-BASH actions), but a useful starting point for users
    who want to rebuild without re-running the agent.

    The agent operates in /workspace with the repo already present, so
    most of its RUN commands (e.g. `pip install -e .`, `pytest`) assume
    repo files are in CWD. We replicate that by setting WORKDIR and
    COPYing the build context in BEFORE replaying the agent's commands.
    Users rebuild with: `docker build -t my-image .` from the repo root.
    """
    lines = [
        "# Auto-generated from r2e-bootstrap agent transcript.",
        "# Build from the cloned repo root: `docker build -t my-image .`",
        f"FROM {base_image}",
        "WORKDIR /workspace",
        "COPY . /workspace",
        "",
    ]
    for t in turns:
        if getattr(t.action, "name", None) == "BASH":
            cmd = t.action.input.replace("\n", " \\\n    ")
            lines.append(f"RUN {cmd}")
    lines.append("")
    return "\n".join(lines)


def ensure_bootstrap(
    repo: RepoSpec,
    spec: BootstrapSpec,
    llm: LLMSpec,
    auth: AuthSpec | None = None,
    *,
    force: bool = False,
    on_turn=None,
    on_thinking=None,
    on_executing=None,
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

    # Surface the budget envelope at startup so users see what's bounding the run.
    logger.info(
        "bootstrap budget: max_iterations=%d max_seconds=%d max_llm_spend_usd=%s",
        spec.max_iterations,
        spec.max_seconds,
        f"${spec.max_llm_spend_usd:.2f}" if spec.max_llm_spend_usd is not None else "unset",
    )

    # Honor user_dockerfile override — skip agent loop entirely
    if spec.user_dockerfile is not None:
        return _bootstrap_from_user_dockerfile(repo, spec, llm, token, force=force)

    def _emit(phase: str, details: dict | None = None) -> None:
        if on_phase is not None:
            try:
                on_phase(phase, details or {})
            except Exception as exc:
                logger.debug("on_phase callback failed: %s", exc)

    with tempfile.TemporaryDirectory(prefix="r2e-clone-") as tmp:
        clone_dir = Path(tmp) / "repo"
        _emit("clone_start", {"detail": f"{repo.url} @ {repo.ref}"})
        logger.info("cloning %s @ %s into %s", repo.url, repo.ref, clone_dir)
        _shallow_clone_at_ref(
            repo.url,
            repo.ref,
            token,
            clone_dir,
            on_progress=lambda line: _emit("clone_progress", {"detail": line}),
        )
        # Scrub the embedded token from .git/config before the clone gets
        # copied into the sandbox and baked into the committed image.
        if token:
            _scrub_clone_credentials(clone_dir, repo.url)
        ref_sha = _resolve_head_sha(clone_dir)
        _emit("clone_done", {"detail": f"{repo.url} @ {ref_sha[:12]}"})

        # Cache check (after we know the resolved SHA)
        owner_name = "/".join(repo.owner_name)
        cache_opts = _cache_options(spec)
        if not force:
            cached = cache_mod.load(owner_name, ref_sha, spec.cache_dir, options=cache_opts)
            if cached is not None and cached.image_digest:
                logger.info("bootstrap cache hit: %s", cached.image_digest)
                for p in ("pull", "sandbox", "agent", "commit"):
                    _emit(f"{p}_skipped", {"detail": "cache hit"})
                _emit("push_skipped", {"detail": "cache hit"})
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

        # Tell any listening UI about the resolved language/base so it can
        # update its header (the CLI fills these in as "unknown" / "ubuntu" at
        # construction time, before the clone has happened).
        if on_phase is not None:
            try:
                on_phase("detected", {"language": lang.value, "base_image": base_image})
            except Exception as exc:
                logger.debug("on_phase callback failed: %s", exc)

        # Spin up sandbox (emits its own pull_*/sandbox_* phase events)
        start = time.monotonic()
        with DockerSandbox.start(
            base_image,
            clone_dir,
            platform=spec.platform,
            on_phase=_emit,
        ) as sandbox:
            _emit("agent_start", {"detail": f"max {spec.max_iterations} iters"})
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
                max_spend_usd=spec.max_llm_spend_usd,
                platform=spec.platform,
                on_turn=on_turn,
                on_thinking=on_thinking,
                on_executing=on_executing,
            )

            # Always persist the transcript — even on failure — for debugging.
            failure_slot = cache_mod.cache_key(
                owner_name, ref_sha, spec.cache_dir, options=cache_opts
            )
            failure_slot.mkdir(parents=True, exist_ok=True)
            try:
                with (failure_slot / "transcript.jsonl").open("w", encoding="utf-8") as f:
                    for turn in outcome.transcript:
                        f.write(
                            json.dumps(
                                {
                                    "step": turn.step,
                                    "thought": turn.thought,
                                    "action": turn.action.name,
                                    "input": turn.action.input,
                                    "observation": turn.observation,
                                }
                            )
                            + "\n"
                        )
            except OSError as exc:
                logger.warning("could not write transcript: %s", exc)

            if not outcome.success:
                _emit("agent_failed", {"detail": outcome.reason[:80]})
                raise BootstrapError(
                    f"bootstrap failed: {outcome.reason} "
                    f"(iterations={outcome.iterations}, cost≈${outcome.total_cost_estimate_usd:.2f}). "
                    f"Transcript at {failure_slot / 'transcript.jsonl'}"
                )
            _emit("agent_done", {"detail": f"{outcome.iterations} iters"})

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
                        r.exit_code,
                        smoke_script[:200],
                    )

            # Make sure git is available in the image. Several base images
            # (python:slim, node:slim, alpine variants) don't ship git, and the
            # agent installs it only when its own build commands need it. But
            # downstream pipelines (pr_runtime / commit_runtime / cve_patches)
            # all need git inside the container to fetch base commits, reset
            # working trees, and apply patches. Adding it here — once,
            # idempotent, captured in the commit — beats every consumer running
            # its own defensive install.
            git_check = sandbox.exec("command -v git >/dev/null 2>&1 && echo OK", timeout=10)
            if not (git_check.ok and "OK" in git_check.stdout):
                _emit("git_install", {"detail": "ensuring git in image"})
                install = sandbox.exec(
                    "(apt-get update >/dev/null 2>&1 && "
                    "apt-get install -y --no-install-recommends git >/dev/null 2>&1 && "
                    "rm -rf /var/lib/apt/lists/*) || "
                    "apk add --no-cache git >/dev/null 2>&1 || true",
                    timeout=180,
                )
                if not install.ok:
                    logger.warning(
                        "post-bootstrap git install returned exit=%d; downstream pipelines "
                        "may need to install git themselves",
                        install.exit_code,
                    )

            # Commit the container regardless — caller decides whether to push
            tag_base = spec.image_registry or "local/r2e-bootstrap"
            owner, name = repo.owner_name
            slug = f"{owner}__{name}".replace("/", "__").lower()
            tag = f"{tag_base}/{slug}:{ref_sha[:12]}".lstrip("/")
            _emit("commit_start", {"detail": tag})
            image_digest = sandbox.commit(tag, message=f"r2e bootstrap {owner_name}@{ref_sha[:12]}")
            _emit("commit_done", {"detail": image_digest[-24:]})

            # Authoritative verify: spin a FRESH container from the committed image
            # and replay test_cmds. Catches "agent declared success in live container
            # but commit dropped some installed state" failure mode.
            _emit("verify_start", {"detail": "fresh container"})
            verify_ok, verify_detail = _verify_committed_image(
                tag, outcome.test_cmds, platform=spec.platform
            )
            if verify_ok:
                _emit("verify_done", {"detail": verify_detail[:80]})
            else:
                _emit("verify_failed", {"detail": verify_detail[:80]})
                logger.warning(
                    "post-commit verify FAILED for %s: %s — image is cached but flagged",
                    tag,
                    verify_detail[:200],
                )

            pushed = False
            if spec.image_registry and "/" in spec.image_registry:
                _emit("push_start", {"detail": tag})
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
                            tag,
                            image_digest,
                        )
                    _emit("push_done", {"detail": image_digest[-24:]})
                else:
                    _emit("push_failed", {"detail": "see logs"})
            else:
                _emit("push_skipped", {"detail": "no --image-registry"})

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
            verify_passed=verify_ok,
            verify_detail=verify_detail,
        )
        cache_mod.save(result, spec.cache_dir, options=cache_opts)

        # Transcript was already written during the agent loop; just link it.
        transcript_path = (
            cache_mod.cache_key(owner_name, ref_sha, spec.cache_dir, options=cache_opts)
            / "transcript.jsonl"
        )
        if transcript_path.exists():
            result.transcript_path = transcript_path
            cache_mod.save(
                result, spec.cache_dir, options=cache_opts
            )  # re-save with transcript path

        logger.info(
            "bootstrap done: %s iterations=%d time=%.1fs digest=%s",
            owner_name,
            outcome.iterations,
            build_time,
            image_digest[:40],
        )
        return result
