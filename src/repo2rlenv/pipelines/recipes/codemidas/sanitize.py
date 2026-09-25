"""Remove upstream tests while retaining empty package scaffolding needed to build."""

from __future__ import annotations

import shlex
import shutil
from pathlib import Path

from repo2rlenv.pipelines.recipes.repository.export import private_asset, repository_build


def public_profile(base, options, private_paths):
    profile = options.model_copy(
        update={"private_test_paths": sorted(set(options.private_test_paths + private_paths))}
    )
    placeholders = []
    for path in sorted(base.rglob("__init__.py")):
        relative = path.relative_to(base)
        if private_asset(relative, profile):
            placeholders.append(relative.as_posix())
    if placeholders:
        parents = sorted({str(Path(path).parent) for path in placeholders})
        scaffold = "mkdir -p -- " + shlex.join(parents) + " && touch -- " + shlex.join(placeholders)
        profile = profile.model_copy(
            update={
                "task_install_command": scaffold
                + " && "
                + (options.task_install_command or options.install_command)
            }
        )
    return profile


def public_build_context(base, profile, destination):
    destination.mkdir(parents=True, exist_ok=False)
    for path in sorted(base.rglob("*")):
        if path.is_symlink():
            raise ValueError("Public snapshots cannot contain symlinks")
        relative = path.relative_to(base)
        if path.is_file() and not private_asset(relative, profile):
            target = destination / "source" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    (destination / "Dockerfile").write_text(repository_build(profile))
