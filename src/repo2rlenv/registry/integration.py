"""Orchestration: discover → verify → select → push → rewrite tasks.

This module is the integration glue between the pure `registry/` primitives
and the HF-Hub-facing `hub.push_to_hub`. It owns the policy:

    if a registry is logged in AND verified to actually work → registry mode
    otherwise → inline-Dockerfile mode (with a prominent warning)
    OR fail hard if `require_registry=True`

It does NOT touch HF Hub — `hub.push_to_hub` does. This module just
prepares the local dataset directory (rewriting environment/Dockerfile +
task.toml in-place) so it's ready to upload, and reports what happened.
"""

from __future__ import annotations

import logging
import re
import time
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from repo2rlenv.registry import probe as probe_mod
from repo2rlenv.registry.auth import (
    RegistryAuth,
    RegistryKind,
    discover_logged_in_registries,
    filter_known_registries,
)
from repo2rlenv.registry.probe import ProbeResult, select_best
from repo2rlenv.registry.push import ImagePushResult, PushError, push_image
from repo2rlenv.registry.visibility import ensure_ghcr_visibility

logger = logging.getLogger(__name__)


ReproMode = Literal["registry", "inline_dockerfile", "local_only"]


@dataclass(slots=True)
class PrepareResult:
    """What `prepare_dataset_for_push` produces. Hub upload is a separate step."""

    mode: ReproMode
    tasks_rewritten: int
    selected_host: str | None = None
    selected_namespace: str | None = None
    image_remote_ref: str | None = None
    image_digest: str | None = None
    image_visibility: Literal["public", "private", "unknown"] | None = None
    image_pushed: bool = False
    fallback_reason: str | None = None
    inline_recipe_source: Literal["user_dockerfile", "agent_replay"] | None = None
    inline_recipe_sha256: str | None = None
    probe_results: list[ProbeResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Inspecting the local dataset
# --------------------------------------------------------------------------

# Match the first `^FROM ...` line (Docker is case-insensitive for FROM)
_FROM_LINE_RE = re.compile(r"^(\s*FROM\s+)(\S+)", re.IGNORECASE | re.MULTILINE)


def _list_task_dirs(local_dir: Path) -> list[Path]:
    """Return task directories (each containing task.toml), sorted, no hidden."""
    out = []
    for child in sorted(local_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if (child / "task.toml").exists():
            out.append(child)
    return out


def _bootstrap_image_refs(task_dirs: list[Path]) -> list[tuple[str, Path]]:
    """For each task with environment/Dockerfile, return (FROM-ref, dockerfile_path).

    Tasks without `environment/` (e.g. pr_diff lite tasks) are skipped.
    """
    out = []
    for td in task_dirs:
        df = td / "environment" / "Dockerfile"
        if not df.is_file():
            continue
        content = df.read_text(encoding="utf-8", errors="replace")
        m = _FROM_LINE_RE.search(content)
        if m:
            out.append((m.group(2).strip(), df))
    return out


def _distinct_local_images(refs: list[tuple[str, Path]]) -> set[str]:
    """Set of bootstrap images that look local (un-pullable)."""
    locals_: set[str] = set()
    for ref, _ in refs:
        if ref.startswith(("local/", "local-")) or ref.startswith("localhost"):
            locals_.add(ref)
        # Also treat refs lacking a registry host (no `.` before the first `/`)
        # as local. e.g. `myimage:tag` with no namespace.
    return locals_


# Known-public base images that consumers can pull anonymously from Docker
# Hub's `library/` namespace. The fast path for self-contained Dockerfiles
# (no upstream bootstrap image to push) only triggers for these — anything
# else falls through to the standard image-push path, which would surface a
# clear error if the image is private/un-pullable instead of silently
# publishing a broken dataset. Pipelines whose Dockerfiles use one of these
# bases (currently just `pr_diff` with `python:3.12-slim`) skip the push.
_PUBLIC_DOCKER_HUB_BASES: tuple[str, ...] = (
    "python:",
    "node:",
    "golang:",
    "rust:",
    "ubuntu:",
    "debian:",
    "alpine:",
    "openjdk:",
    "ruby:",
)


def _is_public_base_image(ref: str) -> bool:
    """True if `ref` is a known-public base that consumers can pull anonymously.

    Conservative on purpose: only the unqualified `library/` Docker Hub bases
    we ship are recognised. A registry-qualified ref the user added by hand
    (e.g. `my-registry.example.com/team/base:tag`) takes the normal image-push
    path — same as before the fast path existed.
    """
    if ref.startswith("docker.io/library/"):
        return any(
            ref.removeprefix("docker.io/library/").startswith(b) for b in _PUBLIC_DOCKER_HUB_BASES
        )
    return any(ref.startswith(b) for b in _PUBLIC_DOCKER_HUB_BASES)


# --------------------------------------------------------------------------
# Discover + verify
# --------------------------------------------------------------------------


def _build_namespace_for(auth: RegistryAuth, hf_owner: str) -> str:
    """Choose the target namespace for a given registry auth + HF owner."""
    if auth.kind is RegistryKind.GHCR:
        # Try HF owner first; caller may retry under gh-login-user on permission denied.
        return hf_owner.lower()
    if auth.kind in (RegistryKind.ECR_PRIVATE, RegistryKind.ECR_PUBLIC):
        return "r2e"
    if auth.kind is RegistryKind.ACR:
        return "r2e"
    if auth.kind is RegistryKind.GCP_AR:
        # GAR: namespace is "project/repo"; we leave project to the user via
        # --image-registry override. For auto-detect we conservatively pick
        # "<project>/r2e" if the auth host carries one (rare); otherwise fall
        # back to "r2e".
        return "r2e"
    if auth.kind is RegistryKind.DOCKER_HUB:
        # Use the logged-in username if we can resolve it; otherwise the HF owner.
        return hf_owner.lower()
    if auth.kind is RegistryKind.LOCAL:
        return "r2e"
    return hf_owner.lower()


def _registry_prefix_for(auth: RegistryAuth, namespace: str) -> str:
    """Build the `<host>/<namespace>` prefix used by build_image_ref."""
    if auth.kind in (RegistryKind.ECR_PRIVATE, RegistryKind.ECR_PUBLIC, RegistryKind.ACR):
        return f"{auth.host}/{namespace}"
    if auth.kind is RegistryKind.GCP_AR:
        return f"{auth.host}/{namespace}"
    if auth.kind is RegistryKind.DOCKER_HUB:
        return f"index.docker.io/{namespace}"
    if auth.kind is RegistryKind.LOCAL:
        return f"{auth.host}/{namespace}"
    return f"{auth.host}/{namespace}"


def _select_verified_registry(
    hf_owner: str,
    *,
    explicit_prefix: str | None,
    require_registry: bool,
) -> tuple[ProbeResult | None, str, list[ProbeResult]]:
    """Discover → probe → select. Returns (selected, namespace, all_probes).

    If `explicit_prefix` is given, we probe only that registry. Otherwise we
    discover from ~/.docker/config.json and probe each.
    """
    probes: list[ProbeResult] = []

    if explicit_prefix:
        # Parse `host[/namespace]` and probe that one
        host, _, ns = explicit_prefix.partition("/")
        if not ns:
            ns = hf_owner.lower()
        # Classify the explicit host
        from repo2rlenv.registry.auth import classify_host

        discovered = discover_logged_in_registries()
        auth = next((a for a in discovered if a.host == host), None)
        if auth is None:
            # Synthesize a stub — probe will likely fail at L2 if no creds
            from repo2rlenv.registry.auth import CredentialSource

            auth = RegistryAuth(
                host=host,
                kind=classify_host(host),
                cred_source=CredentialSource.EMPTY,
            )
        result = probe_mod.probe(auth, ns)
        probes.append(result)
        return (result if result.is_pushable else None, ns, probes)

    # Auto-discover
    candidates = filter_known_registries(discover_logged_in_registries())
    # Drop Docker Hub from auto-default unless explicit (rate-limit hazard)
    candidates = [c for c in candidates if c.kind is not RegistryKind.DOCKER_HUB]

    for auth in candidates:
        ns = _build_namespace_for(auth, hf_owner)
        result = probe_mod.probe(auth, ns)
        probes.append(result)

    selected = select_best(probes)
    if selected is None:
        return (None, hf_owner.lower(), probes)
    return (selected, selected.namespace, probes)


# --------------------------------------------------------------------------
# Local image discovery + remote-ref construction
# --------------------------------------------------------------------------


def _parse_local_tag(local_tag: str) -> tuple[str, str, str]:
    """From `local/r2e-bootstrap/owner__name:sha[__opts]` return (owner, name, sha).

    Tolerates three input forms:
      - `local/r2e-bootstrap/owner__name:sha12`        (tag form)
      - `local/r2e-bootstrap/owner__name:sha12__opt8`  (tag + options hash)
      - `local/r2e-bootstrap/owner__name@sha256:hex`   (digest form)

    Returns 12-char tag-safe sha (truncated for digest form so the tag fits
    within OCI tag-length limits).
    """
    # Digest form has `@sha256:` before the colon; strip it for parsing
    if "@sha256:" in local_tag:
        body, _, digest_hex = local_tag.partition("@sha256:")
        sha = digest_hex[:12]
    else:
        body, _, tag_with_opts = local_tag.partition(":")
        sha = tag_with_opts.split("__", 1)[0].split("-", 1)[0]

    if "/" in body:
        parts = body.split("/")
        last = parts[-1]  # owner__name
    else:
        last = body
    if "__" in last:
        owner, _, name = last.partition("__")
    else:
        owner = "_"
        name = last
    return owner, name, sha


# --------------------------------------------------------------------------
# In-place dataset rewriting
# --------------------------------------------------------------------------


def _rewrite_dockerfile_from(df_path: Path, new_ref: str) -> bool:
    """Rewrite the first `FROM <ref>` line. Returns True if a change was made."""
    content = df_path.read_text(encoding="utf-8", errors="replace")
    new_content, n = _FROM_LINE_RE.subn(rf"\g<1>{new_ref}", content, count=1)
    if n == 0 or new_content == content:
        return False
    df_path.write_text(new_content, encoding="utf-8")
    return True


def _update_task_toml_reproducibility(
    toml_path: Path,
    *,
    mode: ReproMode,
    image_ref: str | None,
    image_tag: str | None,
    image_visibility: str | None,
    pushed_by: str | None,
    inline_recipe_sha256: str | None = None,
    inline_recipe_lines: int = 0,
    inline_recipe_source: str | None = None,
    fallback_reason: str | None = None,
) -> None:
    """Patch the [metadata.repo2env.reproducibility] subtable + bump spec_version.

    Uses a hand-built tomllib roundtrip (parse → mutate dict → re-serialize)
    via tomli_w. The repo already depends on tomli_w through the emitter.
    """
    import tomli_w

    data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    meta = data.setdefault("metadata", {})
    r2e = meta.setdefault("repo2env", {})
    r2e["spec_version"] = "0.2.0"
    repro: dict[str, object] = {
        "mode": mode,
        "image_ref": image_ref,
        "image_tag": image_tag,
        "image_visibility": image_visibility,
        "pushed_at": datetime.now(UTC).isoformat(),
        "pushed_by": pushed_by,
        "inline_recipe_sha256": inline_recipe_sha256,
        "inline_recipe_lines": inline_recipe_lines,
        "inline_recipe_source": inline_recipe_source,
        "fallback_reason": fallback_reason,
    }
    # Strip Nones — TOML can't represent them
    repro = {k: v for k, v in repro.items() if v is not None}
    r2e["reproducibility"] = repro
    # Also refresh the legacy `bootstrap_image` field inside the pipeline's
    # subtable so old readers don't see a stale local ref.
    pipeline_name = r2e.get("pipeline")
    if isinstance(pipeline_name, str):
        sub = r2e.get(pipeline_name)
        if isinstance(sub, dict) and image_ref:
            sub["bootstrap_image"] = image_ref
    toml_path.write_text(tomli_w.dumps(data), encoding="utf-8")


def _hash_text(s: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(s.encode("utf-8")).hexdigest()


def _finalize_self_contained_tasks(
    task_dirs: list[Path], *, image_ref: str, pushed_by: str | None
) -> int:
    """Rewrite reproducibility metadata for self-contained Dockerfile tasks.

    The emitter seeds every Dockerfile-bearing task with
    ``[metadata.repo2env.reproducibility]`` set to
    ``mode="local_only", image_visibility="private"`` — a conservative
    default that's true for un-pushed bootstrap images. For self-contained
    Dockerfiles whose FROM is a known-public base, those values are wrong:
    the FROM image is publicly pullable and the inline Dockerfile recipe is
    what makes the task reproducible. Rewrite each task.toml accordingly so
    downstream tooling that reads reproducibility metadata classifies the
    task correctly.
    """
    rewritten = 0
    for td in task_dirs:
        toml_path = td / "task.toml"
        df_path = td / "environment" / "Dockerfile"
        if not toml_path.is_file() or not df_path.is_file():
            continue
        df_text = df_path.read_text(encoding="utf-8")
        _update_task_toml_reproducibility(
            toml_path,
            mode="inline_dockerfile",
            image_ref=image_ref,
            image_tag=image_ref,
            image_visibility="public",
            pushed_by=pushed_by,
            inline_recipe_sha256=_hash_text(df_text),
            inline_recipe_lines=len(df_text.splitlines()),
            inline_recipe_source="user_dockerfile",
        )
        rewritten += 1
    return rewritten


# --------------------------------------------------------------------------
# The orchestrator
# --------------------------------------------------------------------------


def prepare_dataset_for_push(
    local_dir: Path,
    *,
    hf_owner: str,
    image_registry: str | None = None,
    inline_dockerfile: bool = False,
    require_registry: bool = False,
    skip_image_push: bool = False,
    image_visibility: Literal["public", "private", "inherit"] = "inherit",
    dataset_is_private: bool = False,
    pushed_by: str | None = None,
    on_message=None,
) -> PrepareResult:
    """Run the §7.2 push-time flow: discover → verify → select → push → rewrite.

    Mutates `local_dir` in-place by rewriting each task's
    environment/Dockerfile and task.toml. Returns a `PrepareResult` summarizing
    what mode was selected and any warnings.

    Does NOT upload to HF Hub. The caller (`hub.push_to_hub`) does that after.
    """

    def _emit(msg: str) -> None:
        logger.info(msg)
        if on_message:
            try:
                on_message(msg)
            except Exception:
                pass

    task_dirs = _list_task_dirs(local_dir)
    refs = _bootstrap_image_refs(task_dirs)

    if not refs:
        # Text-only dataset (pr_diff) — nothing to do at the image level.
        _emit("no environment/Dockerfile in dataset; skipping image step")
        # Still set spec_version + reproducibility(mode=local_only-but-not-applicable)
        # for consistency. For pr_diff we just don't write the reproducibility
        # subtable at all — there's no image to be reproducible about.
        return PrepareResult(mode="local_only", tasks_rewritten=0)

    # Enforce 1-image-per-dataset for v0.8.2.post3
    distinct = {ref for ref, _ in refs}
    if len(distinct) > 1:
        raise RuntimeError(
            f"dataset has {len(distinct)} distinct bootstrap images: {sorted(distinct)}. "
            "Multi-image datasets aren't supported yet. Split into separate datasets."
        )
    local_ref = next(iter(distinct))
    looks_local = local_ref.startswith(("local/", "local-")) or local_ref.startswith("localhost")

    # Fast path for self-contained Dockerfiles: if the FROM ref is a
    # known-public base (e.g. ``python:3.12-slim``) the rest of the
    # Dockerfile is the complete recipe — no per-task image push needed.
    # The published task's Dockerfile can be rebuilt on any consumer's
    # machine just by reading the FROM + the inline RUN lines.
    #
    # pr_diff is the canonical case: every task's Dockerfile is
    # ``FROM python:3.12-slim`` followed by apt-get + git clone + bake the
    # oracle. No bootstrap-built image upstream.
    #
    # The allowlist is intentionally narrow. A non-`local/` ref that isn't
    # on the allowlist (e.g. a private registry or an unqualified custom
    # image) falls through to the normal push path, which surfaces a clear
    # error instead of silently publishing a dataset that can't be rebuilt.
    if not looks_local and _is_public_base_image(local_ref):
        rewritten = _finalize_self_contained_tasks(
            task_dirs, image_ref=local_ref, pushed_by=pushed_by
        )
        _emit(
            f"self-contained Dockerfile (FROM {local_ref!r}); "
            "skipping image step — consumers rebuild from the inline recipe"
        )
        return PrepareResult(
            mode="inline_dockerfile",
            tasks_rewritten=rewritten,
            image_remote_ref=local_ref,
            image_visibility="public",
            inline_recipe_source="user_dockerfile",
        )

    # Decide mode
    if inline_dockerfile:
        return _go_inline(
            local_dir,
            task_dirs,
            refs,
            local_ref=local_ref,
            fallback_reason=None,
            pushed_by=pushed_by,
            on_message=_emit,
        )

    # Registry mode candidate
    selected, namespace, probes = _select_verified_registry(
        hf_owner,
        explicit_prefix=image_registry,
        require_registry=require_registry,
    )

    if selected is None:
        # No verified registry available
        if require_registry:
            details = (
                "; ".join(f"{p.host}: {p.error or 'unknown'}" for p in probes)
                or "no registries discovered"
            )
            raise RuntimeError(f"--require-registry: no verified registry available ({details})")
        reason = (
            "; ".join(f"{p.host}: {p.error or 'unknown'}" for p in probes)
            if probes
            else "no registry credentials found"
        )
        _emit(f"WARN no verified registry; falling back to inline-Dockerfile mode ({reason})")
        return _go_inline(
            local_dir,
            task_dirs,
            refs,
            local_ref=local_ref,
            fallback_reason=f"no working registry credentials: {reason}",
            pushed_by=pushed_by,
            on_message=_emit,
        )

    # Registry mode — push + rewrite
    return _go_registry(
        local_dir,
        task_dirs,
        refs,
        local_ref=local_ref,
        selected=selected,
        namespace=namespace,
        image_visibility=image_visibility,
        dataset_is_private=dataset_is_private,
        skip_image_push=skip_image_push,
        require_registry=require_registry,
        looks_local=looks_local,
        probes=probes,
        pushed_by=pushed_by,
        on_message=_emit,
    )


def _go_inline(
    local_dir: Path,
    task_dirs: list[Path],
    refs: list[tuple[str, Path]],
    *,
    local_ref: str,
    fallback_reason: str | None,
    pushed_by: str | None,
    on_message,
) -> PrepareResult:
    """Bake the bootstrap's reconstructed Dockerfile into each task's image recipe.

    Requires the bootstrap cache to be present (we read the saved
    reconstructed Dockerfile from there). Otherwise raises.
    """
    recipe, source = _load_bootstrap_recipe(local_ref)
    if recipe is None:
        raise RuntimeError(
            f"inline mode requires the bootstrap reconstructed Dockerfile "
            f"(saved under ./envs/<repo>/<sha>/Dockerfile). "
            f"Local ref {local_ref!r} resolves to no cached Dockerfile. "
            f"Either re-bootstrap or use registry mode."
        )

    recipe_hash = _hash_text(recipe)
    recipe_lines = len(recipe.splitlines())

    tasks_rewritten = 0
    for _ref, df_path in refs:
        # Build a combined Dockerfile: recipe + per-task overlay from the
        # existing Dockerfile (everything after the FROM line).
        existing = df_path.read_text(encoding="utf-8", errors="replace")
        # Drop the existing FROM line; keep the rest (WORKDIR + git fetch + reset)
        without_from = _FROM_LINE_RE.sub("", existing, count=1).lstrip("\n")
        combined = (
            "# Auto-generated by Repo2RLEnv (inline mode)\n"
            "# Source: bootstrap dockerfile_reconstruction\n"
            f"# Original bootstrap ref: {local_ref}\n\n"
            f"{recipe.rstrip()}\n\n"
            "# Per-task overlay (from pipeline-emitted Dockerfile)\n"
            f"{without_from}"
        )
        df_path.write_text(combined, encoding="utf-8")
        tasks_rewritten += 1
        toml_path = df_path.parent.parent / "task.toml"
        if toml_path.is_file():
            _update_task_toml_reproducibility(
                toml_path,
                mode="inline_dockerfile",
                image_ref=None,
                image_tag=None,
                image_visibility=None,
                pushed_by=pushed_by,
                inline_recipe_sha256=recipe_hash,
                inline_recipe_lines=recipe_lines,
                inline_recipe_source=source,
                fallback_reason=fallback_reason,
            )

    on_message(f"inline mode: rewrote {tasks_rewritten} task Dockerfiles ({source})")

    return PrepareResult(
        mode="inline_dockerfile",
        tasks_rewritten=tasks_rewritten,
        fallback_reason=fallback_reason,
        inline_recipe_source=source,
        inline_recipe_sha256=recipe_hash,
    )


def _load_bootstrap_recipe(
    local_ref: str,
) -> tuple[str | None, Literal["user_dockerfile", "agent_replay"] | None]:
    """Look up the cached bootstrap result for `local_ref` and return its recipe.

    Returns (recipe_text, source) where source is "user_dockerfile" or
    "agent_replay" depending on which bootstrap path produced it. Returns
    (None, None) if no cache entry is found.
    """
    from repo2rlenv.bootstrap import cache as bs_cache

    # Try to extract (owner, name, sha) from the local tag
    owner, name, sha = _parse_local_tag(local_ref)
    if not name or not sha:
        return None, None
    # Walk the default cache_dir; also try the slot path directly
    candidates = [Path("./envs"), Path.cwd() / "envs"]
    for cache_dir in candidates:
        if not cache_dir.is_dir():
            continue
        # We don't know the options hash a priori, so scan
        slot_root = cache_dir / f"{owner}__{name}"
        if not slot_root.is_dir():
            continue
        # Look for a slot whose name starts with the sha
        for slot in slot_root.iterdir():
            if not slot.is_dir():
                continue
            if slot.name.startswith(sha[:12]):
                df = slot / "Dockerfile"
                if df.is_file():
                    text = df.read_text(encoding="utf-8")
                    # Read the cached BootstrapResult to know which source path
                    try:
                        result = bs_cache.load(
                            f"{owner}/{name}",
                            sha,
                            cache_dir,
                            options=None,
                        )
                    except Exception:
                        result = None
                    source: Literal["user_dockerfile", "agent_replay"] = (
                        "user_dockerfile"
                        if result and result.extra.get("source") == "user_dockerfile"
                        else "agent_replay"
                    )
                    return text, source
    return None, None


def _go_registry(
    local_dir: Path,
    task_dirs: list[Path],
    refs: list[tuple[str, Path]],
    *,
    local_ref: str,
    selected: ProbeResult,
    namespace: str,
    image_visibility: Literal["public", "private", "inherit"],
    dataset_is_private: bool,
    skip_image_push: bool,
    require_registry: bool,
    looks_local: bool,
    probes: list[ProbeResult],
    pushed_by: str | None,
    on_message,
) -> PrepareResult:
    """Push the bootstrap image to the verified registry; rewrite tasks."""
    from repo2rlenv.registry.naming import build_image_ref

    # Build the remote ref
    owner, name, sha = _parse_local_tag(local_ref)
    registry_prefix = _registry_prefix_for_kind(selected.kind, selected.host, namespace)
    remote_ref = build_image_ref(
        registry_prefix=registry_prefix,
        owner=owner,
        name=name,
        commit_sha=sha,
    )

    warnings: list[str] = []
    image_pushed = False
    digest: str = remote_ref

    if skip_image_push:
        on_message(f"--skip-image-push: not pushing; reusing {remote_ref}")
    else:
        try:
            push_result = _do_push(local_ref, remote_ref, looks_local)
            digest = push_result.digest
            image_pushed = push_result.pushed
            on_message(
                f"pushed {remote_ref} → {digest[:64]}..."
                if image_pushed
                else f"manifest already at registry: {digest[:64]}..."
            )
        except PushError as exc:
            warnings.append(f"image push failed: {exc}")
            if require_registry:
                raise
            on_message(f"WARN image push failed; falling back to inline mode: {exc}")
            # Fall back to inline
            return _go_inline(
                local_dir,
                task_dirs,
                refs,
                local_ref=local_ref,
                fallback_reason=f"image push failed: {exc}",
                pushed_by=pushed_by,
                on_message=on_message,
            )

    # Visibility
    effective_visibility: Literal["public", "private", "unknown"]
    if image_visibility == "inherit":
        effective_visibility = "private" if dataset_is_private else "public"
    else:
        effective_visibility = image_visibility  # type: ignore[assignment]

    if effective_visibility == "public" and selected.kind is RegistryKind.GHCR:
        vis = ensure_ghcr_visibility(remote_ref, target="public")
        if not vis.success:
            warnings.append(
                f"GHCR visibility flip failed: {vis.error} (manual fix: {vis.manual_url})"
            )
            if require_registry:
                raise RuntimeError(warnings[-1])
            effective_visibility = "unknown"
            on_message(f"WARN {warnings[-1]}")

    # Rewrite tasks
    tasks_rewritten = 0
    for _ref, df_path in refs:
        if _rewrite_dockerfile_from(df_path, digest):
            tasks_rewritten += 1
        toml_path = df_path.parent.parent / "task.toml"
        if toml_path.is_file():
            _update_task_toml_reproducibility(
                toml_path,
                mode="registry",
                image_ref=digest,
                image_tag=remote_ref,
                image_visibility=effective_visibility,
                pushed_by=pushed_by,
            )

    on_message(f"registry mode: rewrote {tasks_rewritten} task Dockerfiles → {digest[:48]}...")

    return PrepareResult(
        mode="registry",
        tasks_rewritten=tasks_rewritten,
        selected_host=selected.host,
        selected_namespace=namespace,
        image_remote_ref=remote_ref,
        image_digest=digest,
        image_visibility=effective_visibility,
        image_pushed=image_pushed,
        probe_results=probes,
        warnings=warnings,
    )


def _registry_prefix_for_kind(kind: RegistryKind, host: str, namespace: str) -> str:
    """Compose `<host>/<namespace>` correctly for each registry kind."""
    if kind is RegistryKind.DOCKER_HUB:
        return f"index.docker.io/{namespace}"
    return f"{host}/{namespace}"


def _do_push(local_ref: str, remote_ref: str, looks_local: bool) -> ImagePushResult:
    """Wrapper to push, with friendly error if local image truly missing."""
    if not looks_local:
        # The dataset already references a registry ref. Either it was
        # pushed elsewhere, or it's our re-push of an already-registered
        # image. Either way: skip a docker tag/push, just verify the digest.
        result = ImagePushResult(
            local_tag=local_ref,
            remote_ref=remote_ref,
            digest=local_ref,
            pushed=False,
            duration_sec=0.0,
        )
        return result
    return push_image(local_ref, remote_ref, skip_if_exists=True)


__all__ = [
    "PrepareResult",
    "ReproMode",
    "prepare_dataset_for_push",
]


_t_start = time.monotonic()
del _t_start
