"""Read-only readiness path checks before dependency builds or GPU allocation."""

from pathlib import Path

from repo2rlenv.spec.recipe_options import PythonRepositoryProfile


def validate_readiness_paths(snapshot: Path, options: PythonRepositoryProfile) -> None:
    """Check selector file prefixes without importing or collecting repository tests."""
    if not snapshot.is_dir():
        raise ValueError("Readiness preflight requires a materialized source snapshot")
    snapshot = snapshot.resolve()
    field = "test_selectors" if options.test_selectors else "test_paths"
    failures = []
    for index, selector in enumerate(getattr(options, field)):
        prefix = selector.split("::", 1)[0]
        path = snapshot / prefix
        if any(
            part.is_symlink() for part in (path, *path.parents) if part.is_relative_to(snapshot)
        ):
            problem = "symlink paths are unsupported"
        elif not (path.is_file() or path.is_dir()):
            problem = "path is absent from the frozen source snapshot"
        else:
            continue
        failures.append(
            f"profile.options.{field}[{index}]={selector!r} (prefix {prefix!r}): {problem}"
        )
    if failures:
        raise ValueError(
            "Readiness preflight failed before dependency build or GPU allocation:\n"
            + "\n".join(failures)
            + "\nSelect existing upstream files/directories for readiness; future private tests "
            "belong in Design.additional_tests. Test node names are checked later by pytest."
        )
