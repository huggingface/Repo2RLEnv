from __future__ import annotations

import json
import time
from pathlib import Path

from repo2rlenv.tasksmith.bootstrap import budgeted_workspace, foundation_recipe
from repo2rlenv.tasksmith.config import TasksmithConfig
from repo2rlenv.tasksmith.providers import BuildSpec, WorkspaceConfig
from repo2rlenv.tasksmith.worker import save_json

NETWORK_PROBE = r"""python -I - <<'PY'
import json, socket
checks = {}
for host in ('github.com', 'huggingface.co', 'pypi.org', '1.1.1.1'):
    try:
        with socket.create_connection((host,443),timeout=3):
            checks[host] = False
    except OSError:
        checks[host] = True
print(json.dumps(checks))
PY"""


async def check_providers(config: TasksmithConfig, root: Path) -> dict:
    """Paid, explicitly invoked CPU profile exercises; no local Docker fallback."""
    root.mkdir(parents=True, exist_ok=True)
    recipe = foundation_recipe()
    results = []
    for provider in ("modal", "daytona"):
        target = root / provider
        completed = target / "conformance.json"
        if completed.exists():
            result = json.loads(completed.read_text())
            if result["recipe"] != recipe:
                raise ValueError("Conformance build inputs changed; use a new profile revision")
            results.append(result)
            continue
        workspace_config = WorkspaceConfig(
            provider=provider,
            operation_id=f"{config.campaign_id}:conformance:{provider}",
            deadline=time.time() + 1200,
            build=BuildSpec(
                kind="recipe", role="bootstrap", files=(("Dockerfile", recipe.encode()),)
            ),
            network="none",
            cpus=2,
            memory_mib=4096,
        )
        result = {"provider": provider, "recipe": recipe, "status": "incomplete", "checks": {}}
        try:
            async with budgeted_workspace(
                workspace_config, target, config.budget("infrastructure")
            ) as remote:
                result["resource_id"] = remote.resource_id
                shell = json.loads(
                    await remote.shell("python -I -c 'import sys; print(sys.version_info[:2])'")
                )
                result["checks"]["python"] = (
                    shell["exit_code"] == 0 and "(3, 12)" in shell["stdout"]
                )
                payload = "Tasksmith remote transfer fixture\n"
                await remote.write("/output/task/fixture.txt", payload)
                result["checks"]["file_roundtrip"] = (
                    await remote.read("/output/task/fixture.txt") == payload.encode()
                )
                manifest = await remote.export(target / "export")
                result["checks"]["export"] = (target / "export/fixture.txt").read_text() == payload
                result["export_manifest"] = manifest
                network = json.loads(await remote.shell(NETWORK_PROBE, timeout_sec=30))
                result["network"] = (
                    json.loads(network["stdout"]) if network["exit_code"] == 0 else {}
                )
                result["checks"]["network"] = len(result["network"]) == 4 and all(
                    result["network"].values()
                )
            receipt = json.loads((target / "resource.json").read_text())
            result["checks"]["cleanup"] = receipt["status"] == "terminated"
            result["receipt"] = receipt
            result["status"] = "passed" if all(result["checks"].values()) else "failed"
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        save_json(completed, result)
        results.append(result)
    report = {
        "profiles": results,
        "passed": all(row["status"] == "passed" for row in results),
        "scope": "CPU dependency build, exec, file transfer, offline network, confirmed teardown; Harbor separate grading validated independently",
    }
    save_json(root / "report.json", report)
    return report
