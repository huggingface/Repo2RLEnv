"""GHCR-specific package visibility helpers.

GHCR creates packages as private by default. To make a public dataset's
bootstrap image pullable without auth, we PATCH the package visibility
via the GitHub REST API:

  PATCH /user/packages/container/<name>            (personal namespace)
  PATCH /orgs/<org>/packages/container/<name>      (org namespace)

The user (or `gh` CLI) needs the `write:packages` scope for this to
succeed. Failure is non-fatal — we warn and continue with private
visibility recorded in the task metadata.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)


VisibilityValue = Literal["public", "private", "internal"]


@dataclass(slots=True)
class VisibilityResult:
    package: str
    target: VisibilityValue
    success: bool
    error: str | None = None
    manual_url: str | None = None


def _gh_available() -> bool:
    return shutil.which("gh") is not None


def _gh_user() -> str | None:
    """Return the authenticated GitHub user login, or None if `gh` unavailable."""
    if not _gh_available():
        return None
    proc = subprocess.run(
        ["gh", "api", "user", "--jq", ".login"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _parse_ghcr_package(remote_ref: str) -> tuple[str, str] | None:
    """From `ghcr.io/<owner>/<name>:<tag>` return (owner, name).

    Returns None if the ref isn't a GHCR ref.
    """
    if not remote_ref.startswith("ghcr.io/"):
        return None
    body = remote_ref.removeprefix("ghcr.io/").split("@", 1)[0].split(":", 1)[0]
    parts = body.split("/", 1)
    if len(parts) != 2:
        return None
    owner, name = parts
    # Package names with embedded `/` are valid in GHCR but encoded as part
    # of the package identifier when hitting the API.
    return owner, name


def ensure_ghcr_visibility(
    remote_ref: str,
    target: VisibilityValue = "public",
) -> VisibilityResult:
    """Flip the GHCR package's visibility (best-effort).

    `remote_ref` can be a full registry ref (`ghcr.io/owner/name:tag` or
    `ghcr.io/owner/name@sha256:...`); we extract owner + package name.

    Idempotent: setting visibility to its existing value returns 204.
    """
    parsed = _parse_ghcr_package(remote_ref)
    if parsed is None:
        return VisibilityResult(
            package=remote_ref,
            target=target,
            success=False,
            error="not a ghcr.io ref",
        )
    owner, name = parsed
    package_full = f"{owner}/{name}"
    if not _gh_available():
        return VisibilityResult(
            package=package_full,
            target=target,
            success=False,
            error="gh CLI not available",
            manual_url=f"https://github.com/users/{owner}/packages/container/{name}/settings",
        )

    # Determine whether this is a user or org namespace.
    me = _gh_user()
    if me and me.lower() == owner.lower():
        api_path = f"/user/packages/container/{name}"
    else:
        api_path = f"/orgs/{owner}/packages/container/{name}"

    proc = subprocess.run(
        [
            "gh",
            "api",
            "-X",
            "PATCH",
            api_path,
            "-f",
            f"visibility={target}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    if proc.returncode == 0:
        return VisibilityResult(
            package=package_full,
            target=target,
            success=True,
        )

    # gh CLI bubbles up the HTTP error body. Extract the message for the user.
    err_body = proc.stderr.strip() or proc.stdout.strip()
    try:
        # Some gh outputs are JSON; others are plain text
        if err_body.startswith("{"):
            err_body = json.loads(err_body).get("message", err_body)
    except (ValueError, json.JSONDecodeError):
        pass
    manual_url = (
        f"https://github.com/users/{owner}/packages/container/{name}/settings"
        if me and me.lower() == owner.lower()
        else f"https://github.com/orgs/{owner}/packages/container/{name}/settings"
    )
    return VisibilityResult(
        package=package_full,
        target=target,
        success=False,
        error=err_body[:400] or f"gh api exit {proc.returncode}",
        manual_url=manual_url,
    )


__all__ = [
    "VisibilityResult",
    "VisibilityValue",
    "ensure_ghcr_visibility",
]
