"""Source-free Python image layers shared by bootstrap and task export."""

from __future__ import annotations

import json
import shlex
from importlib.resources import files


def dependency_recipe(
    base_image: str,
    dependencies: list[str],
    *,
    use_system_site_packages: bool = False,
    hub_assets=(),
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
    if hub_assets:
        assets = [
            asset.model_dump(mode="json", exclude={"purpose", "cache_aliases"})
            for asset in sorted(hub_assets, key=lambda item: item.repo_id)
        ]
        script = files("repo2rlenv.execution").joinpath("hub_asset_fetch.py").read_text()
        script += (
            "\nfetch_assets(json.loads("
            + repr(json.dumps(assets, sort_keys=True))
            + "), Path('/opt/tasksmith-hf/hub'))\n"
        )
        # Encode the program as a Python string to keep the Docker RUN on one
        # line. shlex quotes shell syntax; JSON alone is not shell escaping.
        command = "exec(" + repr(script) + ")"
        recipe += (
            "RUN HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 python -c " + shlex.quote(command) + "\n"
            "ENV HF_HOME=/opt/tasksmith-hf HF_HUB_CACHE=/opt/tasksmith-hf/hub\n"
        )
        aliases = [
            asset.model_dump(mode="json", include={"repo_id", "revision", "cache_aliases"})
            for asset in sorted(hub_assets, key=lambda item: item.repo_id)
            if asset.cache_aliases
        ]
        if aliases:
            script = files("repo2rlenv.execution").joinpath("hub_asset_alias.py").read_text()
            script += (
                "\ninstall_aliases(json.loads("
                + repr(json.dumps(aliases, sort_keys=True))
                + "), Path('/opt/tasksmith-hf/hub'))\n"
            )
            command = "exec(" + repr(script) + ")"
            # Keep aliases after the canonical download layer so changing a
            # lookup name cannot invalidate cached large model downloads.
            recipe += (
                "RUN HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 python -c "
                + shlex.quote(command)
                + "\n"
            )
    return recipe
