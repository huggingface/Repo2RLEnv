"""Image registry support for `repo2rlenv push`.

Unifies push to GHCR / ECR (private + public) / ACR / GCP AR / Docker Hub via
the OCI Distribution Spec. The module is split into:

- `auth`   — read ~/.docker/config.json (incl. credHelpers, credsStore)
- `probe`  — L1-L4 verification protocol against any OCI registry
- `push`   — `docker push` subprocess wrapper + digest extraction
- `visibility` — GHCR-specific package visibility flips
- `ecr`, `acr`, `gar` — vendor-specific helpers (lazy-imported)
- `naming` — slug + tag construction for the bootstrap image

See `plans/v0.8.2.post3_image_distribution.md` for the full design.
"""

from __future__ import annotations

from repo2rlenv.registry.auth import (
    CredentialSource,
    RegistryAuth,
    RegistryKind,
    classify_host,
    discover_logged_in_registries,
)
from repo2rlenv.registry.naming import (
    DEFAULT_BOOTSTRAP_PREFIX,
    build_image_ref,
    slugify_repo,
)

__all__ = [
    "DEFAULT_BOOTSTRAP_PREFIX",
    "CredentialSource",
    "RegistryAuth",
    "RegistryKind",
    "build_image_ref",
    "classify_host",
    "discover_logged_in_registries",
    "slugify_repo",
]
