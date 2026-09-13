"""Standalone repository task materialization with a fresh private verifier."""

from __future__ import annotations

import hashlib
import json
import shlex
from importlib.resources import files
from pathlib import Path, PurePosixPath

from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, write_bundle
from repo2rlenv.execution.python_build import dependency_recipe
from repo2rlenv.spec.recipe_options import PythonRepositoryProfile


def _learner_venv_login() -> str:
    # Harbor's interactive learner starts `bash --login`, whose startup files
    # can replace image ENV PATH. Preserve the original login script, then
    # restore the task runtime after it (even if that script returns early).
    setup = (
        'set -eu; profile="$1/.bash_profile"; original=""; '
        'if [ -e "$profile" ]; then '
        "original=.repo2rlenv-original-bash-profile; "
        'test ! -e "$1/$original"; mv "$profile" "$1/$original"; '
        'elif [ -r "$1/.bash_login" ]; then original=.bash_login; '
        'elif [ -r "$1/.profile" ]; then original=.profile; fi; '
        'if [ -n "$original" ]; then '
        'printf \'if [ -r "$HOME/%s" ]; then . "$HOME/%s"; fi\\n\' '
        '"$original" "$original" > "$profile"; '
        'else : > "$profile"; fi; '
        'printf \'\\nexport PATH="%s/bin:$PATH"\\nexport VIRTUAL_ENV="%s"\\n\' '
        '"$2" "$2" >> "$profile"'
    )
    return (
        "RUN sh -c " + shlex.quote(setup) + " -- /home/learner /opt/tasksmith-venv"
        " && chown learner:learner /home/learner/.bash_profile\n"
    )


def _learner_venv_startup() -> str:
    # Native Harbor exec also uses noninteractive bash, including after `su`.
    # Its runtime hook must not source the learner's interactive/login scripts.
    runtime = 'export PATH="%s/bin:$PATH"\\nexport VIRTUAL_ENV="%s"\\n'
    return (
        _learner_venv_login()
        + "RUN printf "
        + shlex.quote(runtime)
        + " /opt/tasksmith-venv /opt/tasksmith-venv > /etc/repo2rlenv-venv.sh\n"
        "ENV BASH_ENV=/etc/repo2rlenv-venv.sh\n"
    )


def repository_build(options: PythonRepositoryProfile) -> str:
    """Shared install recipe for public readiness and final Harbor images."""
    return (
        dependency_recipe(
            options.base_image,
            options.dependencies,
            use_system_site_packages=options.use_system_site_packages,
            hub_assets=options.hub_assets,
        )
        + "COPY source /workspace\n"
        f"RUN {options.install_command}\n"
        "ENV PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1\n"
        "ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1\n"
    )


def private_asset(relative: PurePosixPath, options: PythonRepositoryProfile) -> bool:
    return any(
        relative == root or root in relative.parents
        for root in map(PurePosixPath, options.test_paths + options.public_exclude)
    )


def export_repository_task(
    *,
    base: Path,
    defective: dict[str, bytes | None],
    reference: dict[str, bytes],
    options: PythonRepositoryProfile,
    instruction: str,
    destination: Path,
    name: str,
    org: str,
    contrast: dict,
    metadata: dict,
    single_reference: bool = False,
    resume: bool = False,
    verifier_source: dict[str, bytes] | None = None,
    collect_source_directories: bool = False,
) -> Path:
    """Materialize a tested repository contrast with private reference files.

    A None baseline value means the PR added that file. Such tasks collect source
    directories and explicitly selected Python files. New helpers are permitted
    within directory roots; selecting one file never expands its parent scope.
    """
    if not defective or defective.keys() != reference.keys():
        raise ValueError("Defective and reference snapshots must replace the same source files")
    assets: dict[str, TaskFile] = {}
    collected = []
    source_roots = [PurePosixPath(path) for path in options.source_paths]
    hidden_roots = [PurePosixPath(path) for path in options.test_paths]
    added = sorted(path for path, content in defective.items() if content is None)
    directory_collection = bool(added) or collect_source_directories
    directory_roots = []
    immutable = {}
    if directory_collection:
        # Directory collection must never replace private tests or hidden assets.
        for root in source_roots:
            if any(
                root != other and (root in other.parents or other in root.parents)
                for other in source_roots
            ):
                raise ValueError("Submitted source directories must not overlap")
            path = base / str(root)
            if path.is_dir():
                directory_roots.append(str(root))
            elif not (path.is_file() and path.suffix == ".py"):
                raise ValueError("Submitted source roots must be directories or Python files")
            if any(
                root == hidden or root in hidden.parents or hidden in root.parents
                for hidden in map(PurePosixPath, options.test_paths + options.public_exclude)
            ):
                raise ValueError("Submitted source directories must not overlap private assets")
    for path in sorted(base.rglob("*")):
        if path.is_symlink():
            raise ValueError("Task snapshots cannot contain symlinks")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("Task snapshots require regular files")
        relative = PurePosixPath(path.relative_to(base).as_posix())
        if (
            any(part in {".git", "__pycache__", ".pytest_cache"} for part in relative.parts)
            or path.suffix == ".pyc"
        ):
            raise ValueError(f"Snapshot contains a forbidden cache/history asset: {relative}")
        content = defective.get(str(relative), path.read_bytes())
        hidden_asset = private_asset(relative, options)
        in_source = any(relative == root or root in relative.parents for root in source_roots)
        if content is not None:
            asset = TaskFile(content, bool(path.stat().st_mode & 0o111))
            assets[f"tests/source/{relative}"] = asset
            if not hidden_asset:
                assets[f"environment/source/{relative}"] = asset
            if directory_collection and in_source and path.suffix != ".py":
                immutable[str(relative)] = hashlib.sha256(content).hexdigest()
        if not hidden_asset and path.suffix == ".py" and in_source:
            collected.append(str(relative))
    if not set(defective).issubset(collected):
        raise ValueError("Changed source must be within the submitted Python source paths")
    if set(collected) & {str(root) for root in hidden_roots}:
        raise ValueError("Source and private test paths overlap")
    for relative, content in (verifier_source or {}).items():
        if relative in collected or f"tests/source/{relative}" in assets:
            raise ValueError("Private verifier additions must not replace repository files")
        assets[f"tests/source/{relative}"] = TaskFile(content)

    build = repository_build(options)
    assets["environment/Dockerfile"] = TaskFile.text(
        build
        + "RUN apt-get update && apt-get install -y --no-install-recommends tmux && rm -rf /var/lib/apt/lists/*\n"
        "RUN useradd -m learner && chown -R learner:learner /workspace\n"
        + (_learner_venv_startup() if options.use_system_site_packages else "")
    )
    assets["tests/Dockerfile"] = TaskFile.text(
        build + "RUN useradd -m -u 1001 grader\n"
        "COPY grade.py test_driver.py test_results.py contract.json test.sh /tests/\n"
        "RUN chmod 755 /tests && chmod 644 /tests/*\n"
    )
    assets["tests/test.sh"] = TaskFile.text(
        "#!/bin/sh\nset -eu\nexec "
        + (
            "/opt/tasksmith-venv/bin/python"
            if options.use_system_site_packages
            else "/usr/local/bin/python"
        )
        + " -I /tests/grade.py\n",
        executable=True,
    )
    assets["tests/grade.py"] = TaskFile(
        files("repo2rlenv.pipelines.recipes.swe_smith").joinpath("grade.py").read_bytes()
    )
    assets["tests/test_driver.py"] = TaskFile(
        files("repo2rlenv.pipelines.recipes.swe_smith").joinpath("test_driver.py").read_bytes()
    )
    assets["tests/test_results.py"] = TaskFile(
        files("repo2rlenv.quality").joinpath("test_results.py").read_bytes()
    )
    assets["tests/contract.json"] = TaskFile.text(
        json.dumps(
            {
                "submitted_files": collected,
                "test_paths": options.test_selectors or options.test_paths,
                "expected_passes": contrast["FAIL_TO_PASS"] + contrast["PASS_TO_PASS"],
                "timeout_sec": options.test_timeout_sec,
                **(
                    {
                        "submitted_roots": directory_roots,
                        "optional_files": added,
                        "immutable_assets": immutable,
                    }
                    if directory_collection
                    else {}
                ),
            },
            sort_keys=True,
        )
    )
    solve = "#!/bin/sh\nset -eu\n"
    for source_file, content in sorted(reference.items()):
        asset = "reference.py" if single_reference else "reference/" + source_file
        assets["solution/" + asset] = TaskFile(content)
        solve += (
            "mkdir -p -- "
            + shlex.quote("/workspace/" + str(PurePosixPath(source_file).parent))
            + "\n"
        )
        solve += "cp " + shlex.quote("/solution/" + asset) + " "
        solve += shlex.quote("/workspace/" + source_file) + "\n"
    assets["solution/solve.sh"] = TaskFile.text(solve, executable=True)
    instruction = instruction.rstrip() + (
        "\n\nWork in `/workspace`. Submit your fix in "
        + (
            "Python source files under "
            if directory_collection
            else "the existing Python source files under "
        )
        + ", ".join(f"`{root}`" for root in options.source_paths)
        + ". Preserve the other public behavior. The environment is offline; dependencies are preinstalled. "
        + (
            "Use `/opt/tasksmith-venv/bin/python` for the preinstalled Python environment. "
            if options.use_system_site_packages
            else ""
        )
        + "Grading runs the relevant repository tests in a fresh environment, using your submitted source files.\n"
    )
    bundle = TaskBundle(
        name=name,
        org=org,
        instruction=instruction,
        files=assets,
        metadata={
            **metadata,
            "reward_kinds": ["test_execution"],
            "quality_status": "exported",
            "fail_to_pass_count": len(contrast["FAIL_TO_PASS"]),
            "pass_to_pass_count": len(contrast["PASS_TO_PASS"]),
        },
        agent={"user": "learner", "network_mode": "no-network"},
        verifier={
            "user": "root",
            "network_mode": "no-network",
            "environment_mode": "separate",
            "environment": {"network_mode": "no-network", "cpus": 1, "memory_mb": 2048},
        },
        artifacts=[
            {
                "source": "/workspace/" + path,
                **(
                    {"exclude": ["__pycache__", "*.pyc", ".pytest_cache"]}
                    if directory_collection
                    else {}
                ),
            }
            for path in (options.source_paths if directory_collection else collected)
        ],
        verifier_timeout_sec=options.test_timeout_sec + 30,
    )
    return write_bundle(bundle, destination, resume=resume)
