"""Source-free Python image layers shared by bootstrap and task export."""

from __future__ import annotations

import shlex


def dependency_recipe(
    base_image: str, dependencies: list[str], *, use_system_site_packages: bool = False
) -> str:
    recipe = f"FROM {base_image}\nWORKDIR /workspace\n"
    if use_system_site_packages:
        recipe += (
            "RUN apt-get update && apt-get install -y --no-install-recommends python3-venv && rm -rf /var/lib/apt/lists/*\n"
            "RUN python -m venv --system-site-packages /opt/tasksmith-venv\n"
            "ENV PATH=/opt/tasksmith-venv/bin:$PATH\n"
        )
    if dependencies:
        recipe += f"RUN python -m pip install --no-cache-dir {shlex.join(dependencies)}\n"
    return recipe
