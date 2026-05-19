"""ECR-specific helper: pre-create a repository before push.

ECR (both private and public) requires the repository to exist before
`docker push` will accept blobs. Unlike GHCR / Docker Hub / ACR which
auto-create repos on first push.

We call the `aws` CLI to keep auth handling out of our codebase —
boto3 would be an extra runtime dep we don't otherwise need.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class ECRError(Exception):
    """Raised when an ECR API call fails irrecoverably."""


@dataclass(slots=True)
class ECRRepoResult:
    repo: str
    created: bool  # True if we just created it; False if it already existed
    is_public: bool


def _aws_available() -> bool:
    return shutil.which("aws") is not None


def _parse_ecr_ref(remote_ref: str) -> tuple[str, str, bool] | None:
    """Return (region_or_alias, repo, is_public) from an ECR ref, or None.

    - Private: `<acct>.dkr.ecr.<region>.amazonaws.com/<repo>[:<tag>]`
    - Public:  `public.ecr.aws/<alias>/<repo>[:<tag>]`
    """
    body = remote_ref.split(":", 1)[0].split("@", 1)[0]
    if body.startswith("public.ecr.aws/"):
        rest = body.removeprefix("public.ecr.aws/")
        if "/" not in rest:
            return None
        alias, repo = rest.split("/", 1)
        return (alias, repo, True)
    # Private form
    if ".dkr.ecr." in body and body.endswith(".amazonaws.com") is False:
        # host part is something like 123.dkr.ecr.us-east-1.amazonaws.com/<repo>
        host, _, repo = body.partition("/")
        if not repo:
            return None
        parts = host.split(".")
        # Expect: ['123', 'dkr', 'ecr', '<region>', 'amazonaws', 'com']
        if len(parts) < 6 or parts[1] != "dkr" or parts[2] != "ecr":
            return None
        region = parts[3]
        return (region, repo, False)
    # Tolerate the alternative split (host first)
    host, _, repo = body.partition("/")
    if not repo or ".dkr.ecr." not in host:
        return None
    parts = host.split(".")
    if len(parts) < 6 or parts[1] != "dkr" or parts[2] != "ecr":
        return None
    region = parts[3]
    return (region, repo, False)


def ensure_ecr_repository(remote_ref: str) -> ECRRepoResult:
    """Idempotent: create the ECR repository for `remote_ref` if it doesn't exist."""
    parsed = _parse_ecr_ref(remote_ref)
    if parsed is None:
        raise ECRError(f"could not parse ECR ref: {remote_ref!r}")
    region_or_alias, repo, is_public = parsed

    if not _aws_available():
        raise ECRError("aws CLI not available on PATH")

    if is_public:
        describe_args = [
            "aws",
            "ecr-public",
            "describe-repositories",
            "--repository-names",
            repo,
            "--region",
            "us-east-1",  # ECR Public is single-region (us-east-1)
        ]
        create_args = [
            "aws",
            "ecr-public",
            "create-repository",
            "--repository-name",
            repo,
            "--region",
            "us-east-1",
        ]
    else:
        describe_args = [
            "aws",
            "ecr",
            "describe-repositories",
            "--repository-names",
            repo,
            "--region",
            region_or_alias,
        ]
        create_args = [
            "aws",
            "ecr",
            "create-repository",
            "--repository-name",
            repo,
            "--region",
            region_or_alias,
        ]

    describe = subprocess.run(
        describe_args, capture_output=True, text=True, timeout=30, check=False
    )
    if describe.returncode == 0:
        return ECRRepoResult(repo=repo, created=False, is_public=is_public)

    create = subprocess.run(create_args, capture_output=True, text=True, timeout=60, check=False)
    if create.returncode != 0:
        raise ECRError(
            f"ECR create-repository failed: {create.stderr.strip() or create.stdout.strip()}"
        )
    return ECRRepoResult(repo=repo, created=True, is_public=is_public)


__all__ = [
    "ECRError",
    "ECRRepoResult",
    "ensure_ecr_repository",
]
