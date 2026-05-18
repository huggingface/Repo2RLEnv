"""Push a local docker image to a remote OCI registry.

Wraps the `docker` CLI rather than docker-py because:

  1. docker-py is an extra runtime dep we don't otherwise need.
  2. The push wire-format is identical across all OCI-compliant registries;
     we just need to invoke `docker push <ref>` and parse RepoDigests.

The push step is idempotent at the registry level: re-pushing the same
local image to the same tag is a no-op for the layers that already exist,
and `RepoDigests` resolves to the same `sha256` content hash.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class PushError(Exception):
    """Raised when `docker push` fails irrecoverably."""


@dataclass(slots=True)
class ImagePushResult:
    local_tag: str
    remote_ref: str
    digest: str  # full `host/path@sha256:...`
    pushed: bool  # False if "already at registry, skipped"
    duration_sec: float


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _run(args: list[str], *, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    """Run a docker subcommand, capturing output and never raising on nonzero."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def manifest_exists(remote_ref: str) -> bool:
    """`docker manifest inspect <ref>` returns 0 iff the registry already has it.

    Used for the idempotent-re-push optimization: skip the push entirely if
    the image is already at the target.
    """
    if not _docker_available():
        return False
    proc = _run(
        ["docker", "manifest", "inspect", remote_ref],
        timeout=60,
    )
    return proc.returncode == 0


def _resolve_repo_digest(remote_ref: str) -> str | None:
    """Return the registry-qualified digest for `remote_ref`, or None.

    `docker image inspect <ref>` exposes RepoDigests AFTER a push (or pull).
    We filter to the digest matching the same registry host so multi-region
    pushes don't get crossed up.
    """
    proc = _run(
        ["docker", "image", "inspect", remote_ref, "--format", "{{json .RepoDigests}}"],
        timeout=30,
    )
    if proc.returncode != 0:
        return None
    try:
        digests = json.loads(proc.stdout.strip() or "[]")
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(digests, list) or not digests:
        return None
    host_prefix = remote_ref.split("/", 1)[0]
    for d in digests:
        if isinstance(d, str) and d.startswith(host_prefix):
            return d
    # Fallback: first digest
    first = digests[0]
    return first if isinstance(first, str) else None


def push_image(
    local_tag: str,
    remote_ref: str,
    *,
    timeout: int = 1800,
    skip_if_exists: bool = True,
) -> ImagePushResult:
    """Tag → push → resolve digest. Returns the canonical registry-qualified ref.

    Raises `PushError` if the local image isn't present, the docker daemon
    isn't running, or the push itself fails.
    """
    import time

    if not _docker_available():
        raise PushError("docker CLI not available on PATH")

    start = time.monotonic()

    # 1. Verify the local image exists
    inspect = _run(
        ["docker", "image", "inspect", local_tag, "--format", "{{.Id}}"],
        timeout=30,
    )
    if inspect.returncode != 0:
        raise PushError(
            f"local image {local_tag!r} not found. "
            "Run `repo2rlenv bootstrap` first, or pass --inline-dockerfile."
        )

    # 2. Optionally skip if already at registry
    if skip_if_exists and manifest_exists(remote_ref):
        logger.info("manifest already at %s; skipping push", remote_ref)
        digest = _resolve_repo_digest(remote_ref) or remote_ref
        return ImagePushResult(
            local_tag=local_tag,
            remote_ref=remote_ref,
            digest=digest,
            pushed=False,
            duration_sec=round(time.monotonic() - start, 2),
        )

    # 3. Tag local → remote
    tag_proc = _run(["docker", "tag", local_tag, remote_ref], timeout=60)
    if tag_proc.returncode != 0:
        raise PushError(
            f"docker tag {local_tag} {remote_ref} failed: {tag_proc.stderr.strip()[:400]}"
        )

    # 4. Push
    push_proc = _run(["docker", "push", remote_ref], timeout=timeout)
    if push_proc.returncode != 0:
        # Surface the actual docker error for the caller to classify (auth
        # denied vs. network vs. repo-doesn't-exist for ECR/GAR).
        raise PushError(f"docker push {remote_ref} failed:\n{push_proc.stderr.strip()[:1000]}")

    # 5. Resolve the canonical digest from RepoDigests
    digest = _resolve_repo_digest(remote_ref)
    if digest is None:
        # Push succeeded but inspect didn't find a digest — degrade to the
        # tagged ref (still pullable, just not pinned).
        digest = remote_ref
        logger.warning("could not resolve RepoDigest for %s; using tag", remote_ref)

    return ImagePushResult(
        local_tag=local_tag,
        remote_ref=remote_ref,
        digest=digest,
        pushed=True,
        duration_sec=round(time.monotonic() - start, 2),
    )


__all__ = [
    "ImagePushResult",
    "PushError",
    "manifest_exists",
    "push_image",
]
