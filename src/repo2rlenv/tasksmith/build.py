"""PR comprehension and construction inside remote author workspaces."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import re
import shlex
import stat
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path, PurePosixPath

from repo2rlenv.tasksmith.authoring import (
    CONSTRUCT,
    DISCOVER,
    PROPOSE,
    Construction,
    Design,
    Discovery,
)
from repo2rlenv.tasksmith.bootstrap import (
    BootstrapCache,
    budgeted_workspace,
    dependency_build,
    foundation_recipe,
)
from repo2rlenv.tasksmith.config import TasksmithConfig
from repo2rlenv.tasksmith.emit import APT_STANZA, emit_task, prepared_recipe
from repo2rlenv.tasksmith.models import safe_relative
from repo2rlenv.tasksmith.providers import BuildSpec, WorkspaceConfig
from repo2rlenv.tasksmith.specification import approve_instruction
from repo2rlenv.tasksmith.worker import artifact_stage, canonical_digest, save_json


def workspace_config(path: Path, **kwargs) -> WorkspaceConfig:
    """Keep the original resource identity and deadline across recovery."""
    if path.exists():
        data = json.loads(path.read_text())
        files = tuple((p, bytes.fromhex(b)) for p, b in data["build"]["files"])
        data["build"]["files"] = files
        data["build"] = BuildSpec(**data["build"])
        retained = WorkspaceConfig(**data)
        # A freshly computed relative workspace timeout must not extend an
        # existing resource's lifetime. Every other configuration field is an
        # immutable identity, including actual build bytes and resource limits.
        if kwargs["deadline"] < retained.deadline:
            raise ValueError("Retained workspace exceeds the requested deadline")
        requested = WorkspaceConfig(**{**kwargs, "deadline": retained.deadline})
        if requested.digest != retained.digest:
            raise ValueError("Workspace identity changed")
        return retained
    config = WorkspaceConfig(**kwargs)
    data = asdict(config)
    data["build"]["files"] = [(p, b.hex()) for p, b in config.build.files]
    save_json(path, data)
    return config


async def prepare_checked(remote, source: dict, root: Path) -> dict:
    path = root / "source-receipt.json"
    if path.exists():
        return json.loads(path.read_text())
    await remote.prepare(source)
    result = json.loads(
        await remote.shell(
            "cd /workspace/repo && git rev-parse "
            + source["base_sha"]
            + "^{tree} "
            + source["head_sha"]
            + "^{tree}"
            + " && git merge-base "
            + source["base_sha"]
            + " "
            + source["head_sha"]
        )
    )
    expected = [source["base_tree_sha"], source["head_tree_sha"], source["base_sha"]]
    if result["exit_code"] or result["stdout"].split() != expected:
        raise ValueError("Remote source trees differ from frozen PR evidence")
    patch = await remote.read("/private/gold.patch")
    (root / "gold.patch").write_bytes(patch)
    receipt = {
        "source": source,
        "trees": expected,
        "patch_digest": hashlib.sha256(patch).hexdigest(),
        "captured_at": time.time(),
    }
    save_json(path, receipt)
    return receipt


async def discover(config: TasksmithConfig, source: dict, root: Path, deadline: float) -> dict:
    result_path = root / "discovery.json"
    if result_path.exists():
        return json.loads(result_path.read_text())
    recipe_path = root / "foundation.Dockerfile"
    root.mkdir(parents=True, exist_ok=True)
    if not recipe_path.exists():
        recipe_path.write_text(await asyncio.to_thread(foundation_recipe))
    recipe = recipe_path.read_text()
    wc = workspace_config(
        root / "workspace-config.json",
        provider=config.provider,
        operation_id=f"{config.campaign_id}:{source['id']}:discover",
        deadline=min(deadline, time.time() + config.workspace_timeout_sec),
        build=BuildSpec(kind="recipe", role="author", files=(("Dockerfile", recipe.encode()),)),
        network="public",
        cpus=2,
        memory_mib=8192,
    )
    async with budgeted_workspace(
        wc, root / "workspace", config.budget(source["id"]), config.cloud_reservation_usd
    ) as remote:
        receipt = await prepare_checked(remote, source, root)

        async def validate(value: Discovery):
            prepared_recipe(value.dependency_dockerfile, source)
            if not value.dependency_dockerfile.startswith(recipe.splitlines()[0]):
                raise ValueError("Use the supplied immutable official Python base")

        artifact = await artifact_stage(
            schema=Discovery,
            stage="discovery",
            inputs=source,
            system=DISCOVER,
            prompt=json.dumps(
                {
                    "source": source,
                    "foundation": recipe,
                    "dependency_recipe_grammar": "Keep FROM, then the exact apt stanza below; remaining lines must be RUN python -m pip install --no-cache-dir name==version ... . CPU torch may use --index-url https://download.pytorch.org/whl/cpu. No other RUN or Docker instructions.",
                    "apt_stanza": APT_STANZA,
                }
            ),
            root=root / "worker",
            budget=config.budget(source["id"]),
            model=config.author_model,
            runtime=config.author_runtime,
            max_cost=config.author_stage_limit_usd,
            max_turns=config.author_turns,
            deadline=wc.deadline,
            shell=remote.shell,
            validate=validate,
        )
        result = {"artifact": artifact.model_dump(mode="json"), "source_receipt": receipt}
        save_json(result_path, result)
    return result


class BootstrapReadinessError(RuntimeError):
    """Retained readiness failure: constructing on this image is not authorized."""


def _pytest_command(command):
    tokens = shlex.split(command)
    if tokens[:3] in (["python", "-m", "pytest"], ["python3", "-m", "pytest"]):
        args = tokens[3:]
    elif tokens[:1] == ["pytest"]:
        args = tokens[1:]
    else:
        raise ValueError("Selector correction requires a direct python -m pytest/pytest command")
    modules, index = set(), 0
    flags = {"-q", "-v", "-vv", "-s", "-x", "--disable-warnings", "--strict-markers"}
    valued = {"-k", "-m", "--maxfail", "--tb", "--capture"}
    while index < len(args):
        token = args[index]
        if token in valued:
            index += 1
            if index >= len(args):
                raise ValueError("Pytest option requires a value")
        elif token in flags or any(token.startswith(flag + "=") for flag in valued):
            pass
        elif not token.startswith("-"):
            module = token.split("::", 1)[0]
            if not re.fullmatch(r"[A-Za-z0-9_./-]+\.py", module) or any(
                part in {"", ".", ".."} for part in module.split("/")
            ):
                raise ValueError(
                    "Selector correction requires explicit relative Python test modules"
                )
            modules.add(module)
        else:
            raise ValueError(f"Unsupported selector-repair pytest option: {token}")
        index += 1
    if not modules:
        raise ValueError("Selector correction must retain explicit upstream module files")
    return args, modules


def _selector_failure(discovered, readiness):
    if readiness.get("passed"):
        return False
    for row in readiness.get("checks", []):
        if row.get("command") not in discovered.upstream_test_commands or row.get(
            "exit_code"
        ) not in {4, 5}:
            continue
        output = str(row.get("stdout", "")) + "\n" + str(row.get("stderr", ""))
        if re.search(
            r"(?i)(ERROR:\s*(?:not found|file or directory not found)|no tests (?:ran|collected)|collected 0 items|found no collectors)",
            output,
        ):
            return True
    return False


def _test_definitions(text, module):
    tree, found = ast.parse(text), {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test"
        ):
            found[f"{module}::{node.name}"] = ast.dump(node, include_attributes=False)
        elif isinstance(node, ast.ClassDef):
            for method in node.body:
                if isinstance(
                    method, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and method.name.startswith("test"):
                    found[f"{module}::{node.name}::{method.name}"] = ast.dump(
                        method, include_attributes=False
                    )
    return found


async def _pytest_observation(shell, source, command, *, collect_only):
    args, _ = _pytest_command(command)
    if collect_only:
        args = [*args, "--collect-only"]
    # Trusted observation wrapper runs remotely. No target/test imports occur on the controller.
    script = """import contextlib,io,json,sys,pytest
class Observe:
    def __init__(self): self.collected=[]; self.executed=[]
    def pytest_collection_finish(self,session): self.collected=[item.nodeid for item in session.items]
    def pytest_runtest_logreport(self,report):
        if report.when == 'call' and not report.skipped: self.executed.append(report.nodeid)
p=Observe(); out=io.StringIO(); err=io.StringIO()
with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
    code=int(pytest.main(ARGS,plugins=[p]))
print('__TASKSMITH_PYTEST__'+json.dumps({'exit_code':code,'collected':p.collected,'executed':p.executed,'stdout':out.getvalue()[-12000:],'stderr':err.getvalue()[-12000:]}))
sys.exit(code)
""".replace("ARGS", repr(args))
    observed = json.loads(
        await shell(
            "cd /workspace/repo && git reset --hard "
            + shlex.quote(source["head_sha"])
            + " && git clean -fd && python -c "
            + shlex.quote(script),
            300,
        )
    )
    marker = "__TASKSMITH_PYTEST__"
    lines = [
        line[len(marker) :]
        for line in observed.get("stdout", "").splitlines()
        if line.startswith(marker)
    ]
    if len(lines) != 1:
        raise BootstrapReadinessError(
            "Pytest observation report missing; collection/execution is unproven"
        )
    report = json.loads(lines[0])
    if (
        report.get("exit_code") != observed.get("exit_code")
        or not isinstance(report.get("collected"), list)
        or not isinstance(report.get("executed"), list)
        or any(
            not isinstance(node, str) or not node
            for node in report["collected"] + report["executed"]
        )
    ):
        raise BootstrapReadinessError("Pytest observation report is inconsistent")
    return report


async def _selector_sources(shell, source, discovered, root):
    path = root / "source-and-collection.json"
    modules = sorted(
        {
            module
            for command in discovered.upstream_test_commands
            for module in _pytest_command(command)[1]
        }
    )
    identity = canonical_digest({"source": source, "modules": modules})
    if path.exists():
        evidence = json.loads(path.read_text())
        if evidence.get("input_digest") != identity:
            raise BootstrapReadinessError(
                "Selector source evidence belongs to different source/modules"
            )
        collection = evidence.get("full_collection", {})
        if collection.get("exit_code") != 0 or not collection.get("collected"):
            raise BootstrapReadinessError(
                "Retained full-module collection failed; inspect " + str(path)
            )
        return evidence
    if shell is None:
        raise BootstrapReadinessError(
            "Selector correction needs remote source and collection observations"
        )
    if len(modules) > 8:
        raise BootstrapReadinessError("Selector repair exceeds the bounded eight-module inspection")
    files, changed, total = {}, set(), 0
    for module in modules:
        versions = {}
        for label, commit in (("before", source["base_sha"]), ("head", source["head_sha"])):
            observed = json.loads(
                await shell(
                    "cd /workspace/repo && git show " + shlex.quote(commit + ":" + module), 120
                )
            )
            if observed["exit_code"] and label == "head":
                raise BootstrapReadinessError(f"Pinned upstream module is unavailable: {module}")
            versions[label] = observed["stdout"] if observed["exit_code"] == 0 else ""
        total += sum(len(value.encode()) for value in versions.values())
        if total > 128_000:
            raise BootstrapReadinessError(
                "Selector source inspection exceeds 128 KB; no evidence was silently dropped"
            )
        old, new = (_test_definitions(versions[label], module) for label in ("before", "head"))
        changed.update(name for name, value in new.items() if old.get(name) != value)
        files[module] = versions
    collection = await _pytest_observation(
        shell, source, "python -m pytest " + shlex.join(modules) + " -q", collect_only=True
    )
    available = {node.split("[", 1)[0] for node in collection["collected"]}
    evidence = {
        "input_digest": identity,
        "modules": modules,
        "source_files": files,
        "full_collection": collection,
        "required_changed_anchors": sorted(changed & available),
    }
    save_json(path, evidence)
    if collection["exit_code"] or not collection["collected"]:
        raise BootstrapReadinessError(
            "Full upstream modules did not collect successfully; selector repair cannot establish coverage; "
            "inspect " + str(path)
        )
    return evidence


async def _selector_collection(shell, source, corrected, evidence, root):
    path = root / "selection-proof.json"
    identity = canonical_digest(
        {"source_evidence": evidence, "commands": corrected.upstream_test_commands}
    )
    if path.exists():
        proof = json.loads(path.read_text())
        if proof.get("input_digest") == identity:
            return proof
    if shell is None:
        raise BootstrapReadinessError("Corrected selectors lack retained collection proof")
    reports = []
    check_path = root / "selection-checks" / f"{identity}.json"
    for command in corrected.upstream_test_commands:
        report = await _pytest_observation(shell, source, command, collect_only=True)
        reports.append({"command": command, **report})
        if report["exit_code"] or not report["collected"]:
            save_json(
                check_path, {"input_digest": identity, "collections": reports, "passed": False}
            )
            raise ValueError("Corrected upstream command must collect nonzero tests successfully")
    selected = {node.split("[", 1)[0] for report in reports for node in report["collected"]}
    missing = set(evidence["required_changed_anchors"]) - selected
    if missing:
        save_json(
            check_path,
            {
                "input_digest": identity,
                "collections": reports,
                "passed": False,
                "missing_changed_anchors": sorted(missing),
            },
        )
        raise ValueError(
            f"Corrected selectors omit changed collectable upstream tests: {sorted(missing)}"
        )
    proof = {
        "input_digest": identity,
        "commands": corrected.upstream_test_commands,
        "required_changed_anchors": evidence["required_changed_anchors"],
        "collections": reports,
    }
    save_json(check_path, {**proof, "passed": True})
    save_json(path, proof)
    return proof


def _readiness_inputs(source, discovered, key, selector_proof=None):
    commands = [
        "git checkout --detach "
        + source["head_sha"]
        + " && python -m pip install --no-index --no-deps --no-build-isolation -e .",
        *discovered.readiness_commands,
        *discovered.upstream_test_commands,
    ]
    inputs = {"source": source, "commands": commands, "cache_key": key}
    if selector_proof is not None:
        inputs["selector_proof"] = selector_proof
    return inputs


async def _reference_readiness(
    remote, source, discovered, root, *, key, cached, selector_proof=None
):
    inputs = _readiness_inputs(source, discovered, key, selector_proof)
    commands, identity = inputs["commands"], canonical_digest(inputs)
    path = root / "readiness.json"
    if path.exists():
        readiness = json.loads(path.read_text())
        if readiness.get("input_digest") != identity:
            raise BootstrapReadinessError(
                "Retained readiness belongs to different source, commands or dependencies"
            )
        return readiness
    outputs, execution_errors, executed = [], [], set()
    for command in commands:
        if selector_proof is not None and command in discovered.upstream_test_commands:
            observed = await _pytest_observation(remote.shell, source, command, collect_only=False)
            executed.update(node.split("[", 1)[0] for node in observed["executed"])
            if not observed["executed"]:
                execution_errors.append(
                    f"Corrected command executed no non-skipped tests: {command}"
                )
        else:
            observed = json.loads(await remote.shell("cd /workspace/repo && " + command, 300))
        outputs.append({"command": command, **observed})
        if observed["exit_code"] or execution_errors:
            break
    if selector_proof is not None:
        missing = set(selector_proof["required_changed_anchors"]) - executed
        if missing:
            execution_errors.append(f"Changed upstream tests were not executed: {sorted(missing)}")
    reset = json.loads(
        await remote.shell(
            "cd /workspace/repo && git reset --hard " + source["base_sha"] + " && git clean -fd",
            120,
        )
    )
    readiness = {
        "input_digest": identity,
        "checks": outputs,
        "reset": reset,
        "passed": len(outputs) == len(commands)
        and all(row["exit_code"] == 0 for row in outputs)
        and not execution_errors
        and reset["exit_code"] == 0,
        "cache_key": key,
        "cache_hit": bool(cached),
    }
    if selector_proof is not None:
        readiness["selector_proof"] = selector_proof
        readiness["execution_errors"] = execution_errors
    save_json(path, readiness)
    return readiness


async def _correct_discovery(
    config, source, discovered, readiness, root, *, budget, deadline, shell, selector=False
):
    original = discovered.model_dump(mode="json")
    inputs = {"source": source, "discovery": original, "failed_readiness": readiness}
    stage = "bootstrap-selector-repair-1" if selector else "bootstrap-repair-1"
    if selector:
        if not _selector_failure(discovered, readiness):
            raise BootstrapReadinessError(
                "Selector repair requires explicit pytest selection failure"
            )
        evidence = await _selector_sources(shell, source, discovered, root / stage)
        inputs["upstream_source_and_collection"] = evidence

    async def validate(value: Discovery):
        prepared_recipe(value.dependency_dockerfile, source)
        if selector:
            for field in (
                "dependency_dockerfile",
                "dependency_inputs",
                "useful_outcome",
                "behaviors",
                "readiness_commands",
                "upstream_tests",
            ):
                if value.model_dump(mode="json")[field] != original[field]:
                    raise ValueError(f"Selector repair cannot weaken or replace {field}")
            if value.upstream_test_commands == discovered.upstream_test_commands:
                raise ValueError("Selector repair must correct the failed upstream command")
            modules = {
                module
                for command in value.upstream_test_commands
                for module in _pytest_command(command)[1]
            }
            if modules != set(evidence["modules"]):
                raise ValueError(
                    "Selector repair must preserve exactly the same upstream module files"
                )
            await _selector_collection(shell, source, value, evidence, root / stage)
            return
        if (
            value.dependency_dockerfile.splitlines()[0]
            != discovered.dependency_dockerfile.splitlines()[0]
        ):
            raise ValueError("Dependency repair must preserve the original immutable Python base")
        if value.dependency_dockerfile == discovered.dependency_dockerfile:
            raise ValueError(
                "Dependency repair must change the reproducible recipe, not only the live shell"
            )
        for field in (
            "useful_outcome",
            "behaviors",
            "readiness_commands",
            "upstream_test_commands",
            "upstream_tests",
        ):
            if value.model_dump(mode="json")[field] != original[field]:
                raise ValueError(f"Dependency repair cannot weaken or replace {field}")

    guidance = (
        "\nCorrect only the pytest selectors from the explicit retained selection failure. "
        "Use the pinned upstream module source and actual collection node IDs supplied below. "
        "Preserve the useful outcome, behaviors, readiness commands, dependency recipe and inputs, "
        "and exactly the same upstream module files. Include every changed collectable test anchor "
        "in those modules; selecting unrelated or only older tests is not a correction. Return a "
        "complete Discovery with corrected upstream_test_commands. The host will collect the "
        "selection and then execute nonzero tests in this same workspace before proceeding. "
        "No shell-only installation, source edit, new image or claimed pass is part of this repair."
        if selector
        else "\nRepair only the dependency bootstrap from the retained failure. Preserve the useful outcome, behaviors, exact readiness and upstream test commands. Return a complete corrected Discovery. Preserve the immutable base and dependency-only grammar. Explain changed pins/build support through dependency_inputs. Shell-only installations are diagnostic, never the repair: a fresh image must pass the same checks. Do not claim reference success before the controller rebuilds and reruns it."
    )
    correction = await artifact_stage(
        schema=Discovery,
        stage=stage,
        inputs=inputs,
        system=DISCOVER + guidance,
        prompt=json.dumps({**inputs, "apt_stanza": APT_STANZA, "attempt_limit": 2}),
        root=root / stage,
        budget=budget,
        model=config.author_model,
        runtime=config.author_runtime,
        max_cost=config.author_stage_limit_usd,
        max_turns=config.author_turns,
        deadline=deadline,
        # Host observations supply the exact source and collection. A selector-only
        # correction must not mutate the otherwise valid dependency environment.
        shell=None if selector else shell,
        validate=validate,
    )
    # artifact_stage's retained-output path parses the schema but does not rerun callbacks.
    await validate(correction)
    return correction


@asynccontextmanager
async def bootstrap_ready(config, source, discovered, root, cache_root, deadline, budget):
    """At most two builds and one in-place selector correction, retaining every failed check."""
    policy_path = root / "bootstrap-policy.json"
    identity = canonical_digest({"source": source, "discovery": discovered.model_dump(mode="json")})
    if policy_path.exists():
        policy = json.loads(policy_path.read_text())
        if policy.get("input_digest") != identity or policy.get("candidate_deadline") != deadline:
            raise BootstrapReadinessError("Bootstrap inputs or original candidate deadline changed")
    else:
        policy = {
            "input_digest": identity,
            "candidate_deadline": deadline,
            "deadline": min(deadline, time.time() + config.workspace_timeout_sec),
            "attempt_limit": 2,
        }
        save_json(policy_path, policy)
    bootstrap_deadline = policy["deadline"]
    cache = BootstrapCache(cache_root)
    selector_proof = None
    for attempt in range(2):
        if time.time() >= bootstrap_deadline:
            raise TimeoutError(
                "Original bootstrap deadline exhausted; retained attempts cannot reset it"
            )
        attempt_root = root if attempt == 0 else root / "bootstrap-attempt-2"
        attempt_root.mkdir(parents=True, exist_ok=True)
        recipe = discovered.dependency_dockerfile
        prepared_recipe(recipe, source)
        key = cache.identity(recipe, discovered.dependency_inputs)
        failed_path = attempt_root / "readiness.json"
        retained = json.loads(failed_path.read_text()) if failed_path.exists() else None
        if retained and not retained.get("passed"):
            if retained.get("input_digest") != canonical_digest(
                _readiness_inputs(source, discovered, key, selector_proof)
            ):
                raise BootstrapReadinessError("Retained failed readiness belongs to changed inputs")
            if _selector_failure(discovered, retained):
                raise BootstrapReadinessError(
                    "Retained selector failure needs explicit workspace reconciliation; "
                    "it cannot authorize a dependency rebuild or reset the selector attempt"
                )
            resource_path = attempt_root / "workspace/resource.json"
            resource = json.loads(resource_path.read_text()) if resource_path.exists() else {}
            if resource.get("status") != "terminated":
                raise BootstrapReadinessError(
                    "Previous failed bootstrap workspace cleanup is unconfirmed; reconcile before rebuilding"
                )
            if attempt:
                raise BootstrapReadinessError(
                    f"Reference readiness still failed after two bootstrap attempts; inspect {failed_path}"
                )
            # No second sandbox is needed to recover an already committed correction.
            discovered = await _correct_discovery(
                config,
                source,
                discovered,
                retained,
                root,
                budget=budget,
                deadline=bootstrap_deadline,
                shell=None,
            )
            continue
        async with cache.claim(key):
            cached = cache.lookup(key, config.provider)
            wc = workspace_config(
                attempt_root / "workspace-config.json",
                provider=config.provider,
                operation_id=f"{config.campaign_id}:{source['id']}:{root.name}"
                + (":bootstrap-2" if attempt else ""),
                deadline=bootstrap_deadline,
                build=dependency_build(recipe, cached, config.provider),
                network="public",
                cpus=2,
                memory_mib=8192,
            )
            async with budgeted_workspace(
                wc, attempt_root / "workspace", budget, config.cloud_reservation_usd
            ) as remote:
                await prepare_checked(remote, source, attempt_root)
                readiness = await _reference_readiness(
                    remote,
                    source,
                    discovered,
                    attempt_root,
                    key=key,
                    cached=cached,
                    selector_proof=selector_proof,
                )
                if _selector_failure(discovered, readiness):
                    if selector_proof is not None:
                        raise BootstrapReadinessError(
                            "The one corrected selector attempt still failed"
                        )
                    discovered = await _correct_discovery(
                        config,
                        source,
                        discovered,
                        readiness,
                        root,
                        budget=budget,
                        deadline=bootstrap_deadline,
                        shell=remote.shell,
                        selector=True,
                    )
                    selector_proof = json.loads(
                        (root / "bootstrap-selector-repair-1/selection-proof.json").read_text()
                    )
                    selection_root = attempt_root / "selector-readiness-1"
                    readiness = await _reference_readiness(
                        remote,
                        source,
                        discovered,
                        selection_root,
                        key=key,
                        cached=cached,
                        selector_proof=selector_proof,
                    )
                    failed_path = selection_root / "readiness.json"
                save_json(
                    root / "bootstrap-status.json",
                    {
                        "attempt": attempt + 1,
                        "passed": readiness["passed"],
                        "readiness_path": str(failed_path),
                        "cache_key": key,
                    },
                )
                if readiness["passed"]:
                    if not cached:
                        current = json.loads((attempt_root / "workspace/resource.json").read_text())
                        cache.save(
                            key, config.provider, recipe, discovered.dependency_inputs, current
                        )
                    yield discovered, wc, readiness, remote
                    return
                if _selector_failure(discovered, readiness) or readiness.get("execution_errors"):
                    raise BootstrapReadinessError(
                        f"Corrected upstream selection did not execute required tests; inspect {failed_path}"
                    )
                if attempt == 0:
                    discovered = await _correct_discovery(
                        config,
                        source,
                        discovered,
                        readiness,
                        root,
                        budget=budget,
                        deadline=bootstrap_deadline,
                        shell=remote.shell,
                    )
            # budgeted_workspace confirms previous cleanup before another image is built.
        resource = json.loads((attempt_root / "workspace/resource.json").read_text())
        if resource.get("status") != "terminated":
            raise BootstrapReadinessError(
                "Failed bootstrap workspace cleanup is unconfirmed; no rebuild"
            )
        if attempt:
            raise BootstrapReadinessError(
                f"Reference readiness still failed after two bootstrap attempts; inspect {failed_path}"
            )


def load_recovery(root: Path, source: dict) -> dict | None:
    """Read a controller-authorized construction handoff as bounded, untrusted data.

    This restores partial author work; it never establishes task validity or
    authorizes retry of an unreconciled journal operation.
    """
    match = re.fullmatch(r"revision-(\d+)-attempt-([1-9]\d*)", root.name)
    if match is None:
        return None
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError("Construction recovery target must not contain symlinks")
    path = root.parent / f"recovery-construction-revision-{match[1]}-attempt-{match[2]}.json"
    # The original handoff filename predated semantic revisions. It belongs to
    # revision zero only; another revision's attempt1 must not consume it.
    if not path.exists() and not path.is_symlink() and match[1] == "0":
        path = root.parent / f"recovery-construction-attempt-{match[2]}.json"

    def regular_bytes(file: Path, limit: int) -> bytes:
        if any(p.is_symlink() for p in (file, *file.parents)):
            raise ValueError("Construction recovery paths must not contain symlinks")
        info = file.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("Construction recovery file is nonregular or oversized")
        with file.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) != info.st_size or len(data) > limit:
            raise ValueError("Construction recovery file changed or exceeds its bound")
        return data

    if not path.exists() and not path.is_symlink():
        return None
    receipt = json.loads(regular_bytes(path, 1_000_000))
    required = {
        "source_digest",
        "target_root_name",
        "original_stage_root",
        "files",
        "last_submission",
        "design",
        "reason",
    }
    if not isinstance(receipt, dict) or not required.issubset(receipt):
        raise ValueError("Incomplete construction recovery receipt")
    if (
        receipt["source_digest"] != canonical_digest(source)
        or receipt["target_root_name"] != root.name
    ):
        raise ValueError("Construction recovery source or target identity differs")
    original = Path(receipt["original_stage_root"])
    original_match = re.fullmatch(r"revision-(\d+)(?:-attempt-([1-9]\d*))?", original.name)
    if (
        not original.is_absolute()
        or original.parent != root.absolute().parent
        or any(p.is_symlink() for p in (original, *original.parents))
        or not original.is_dir()
        or original_match is None
        or original_match[1] != match[1]
        or int(original_match[2] or 0) >= int(match[2])
    ):
        raise ValueError("Construction recovery must name an earlier stage of this revision")
    if (
        not isinstance(receipt["last_submission"], dict)
        or not isinstance(receipt["reason"], str)
        or not receipt["reason"].strip()
    ):
        raise ValueError("Construction recovery needs the failed submission and causal reason")
    design = Design.model_validate(receipt["design"])
    if "bootstrap_discovery" in receipt:
        Discovery.model_validate(receipt["bootstrap_discovery"])
    rows = receipt["files"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 150:
        raise ValueError("Construction recovery requires 1 to 150 bounded files")
    files, names, total = [], set(), 0
    for row in rows:
        if not isinstance(row, dict) or not {"path", "local_path", "sha256", "size"}.issubset(row):
            raise ValueError("Incomplete construction recovery file entry")
        name = row["path"]
        if not isinstance(name, str) or not name or "\\" in name or any(ord(c) < 32 for c in name):
            raise ValueError("Unsafe construction recovery relative path")
        relative = PurePosixPath(name)
        if (
            relative.is_absolute()
            or str(relative) != name
            or any(part in {".", ".."} for part in relative.parts)
            or name == "."
        ):
            raise ValueError("Unsafe construction recovery relative path")
        if name in names:
            raise ValueError("Duplicate construction recovery file path")
        names.add(name)
        if type(row["size"]) is not int or not 0 <= row["size"] <= 2_000_000:
            raise ValueError("Construction recovery file exceeds 2 MB")
        total += row["size"]
        if total > 20_000_000:
            raise ValueError("Construction recovery exceeds 20 MB")
        local = Path(row["local_path"])
        if not local.is_absolute():
            raise ValueError("Construction recovery local paths must be absolute")
        data = regular_bytes(local, 2_000_000)
        if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ValueError("Construction recovery file size or SHA256 differs")
        files.append((name, data))
    if any(str(parent) in names for name in names for parent in PurePosixPath(name).parents):
        raise ValueError("Overlapping construction recovery file paths")
    return {
        "receipt": receipt,
        "receipt_digest": canonical_digest(receipt),
        "files": files,
        "design": design,
    }


def load_prior_construction(
    root: Path, source: dict, previous: dict, parent_task_digest: str
) -> dict:
    """Capture the preceding immutable task as data for a new semantic revision."""
    from repo2rlenv.tasksmith.emit import _dependency_recipe, _source

    if len(json.dumps(previous, allow_nan=False).encode()) > 1_000_000:
        raise ValueError("Prior construction metadata exceeds 1 MB")
    current = re.fullmatch(r"revision-(\d+)(?:-attempt-[1-9]\d*)?", root.name)
    task = Path(previous["task_path"])
    prior = re.fullmatch(r"revision-(\d+)(?:-attempt-[1-9]\d*)?", task.parent.name)
    if (
        current is None
        or prior is None
        or int(current[1]) != int(prior[1]) + 1
        or previous.get("task_revision", int(prior[1])) != int(prior[1])
        or task.name != "task"
        or not task.is_absolute()
        or task.parent.parent != root.absolute().parent
        or any(p.is_symlink() for p in (root, *root.parents, task, *task.parents))
        or not task.is_dir()
    ):
        raise ValueError("Semantic repair must use the preceding revision in this candidate")
    emitter = previous["emitter"]
    if emitter["task_digest"] != parent_task_digest or emitter.get("source") != _source(source):
        raise ValueError("Prior construction source pins or parent digest differ")
    discovery = Discovery.model_validate(previous["bootstrap_discovery"])
    recipe_digest = hashlib.sha256(
        _dependency_recipe(discovery.dependency_dockerfile, _source(source)).encode()
    ).hexdigest()
    if recipe_digest != emitter["dependency_recipe_digest"]:
        raise ValueError("Prior bootstrap recipe differs from the emitted dependency identity")
    manifest = Construction.model_validate(previous["artifact"])
    design = Design.model_validate(previous["design"])
    files, pending, entries, total = {}, [task], 0, 0
    while pending:
        for file in sorted(pending.pop().iterdir()):
            entries += 1
            if entries > 500:
                raise ValueError("Prior task exceeds 500 filesystem entries")
            info = file.lstat()
            if stat.S_ISDIR(info.st_mode):
                pending.append(file)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Prior task contains linked or nonregular files")
            total += info.st_size
            if info.st_size > 2_000_000 or total > 20_000_000 or len(files) >= 150:
                raise ValueError("Prior task exceeds bounded file-transfer limits")
            with file.open("rb") as stream:
                data = stream.read(2_000_001)
            if len(data) != info.st_size:
                raise ValueError("Prior task file changed during capture")
            files[file.relative_to(task).as_posix()] = data
    digest = hashlib.sha256()
    # Match digest_task's Path ordering, including file/directory prefix cases.
    for name, data in sorted(files.items(), key=lambda row: Path(row[0])):
        digest.update(name.encode() + b"\0" + data + b"\0")
    if digest.hexdigest() != parent_task_digest:
        raise ValueError("Prior task digest changed before semantic repair")
    if json.loads(files["contract.json"]) != manifest.contract.model_dump(mode="json"):
        raise ValueError("Prior authored contract differs from the emitted contract")
    if files.get("tests/source_origin_probe.py", b"").decode() != manifest.source_origin_probe:
        raise ValueError("Prior source probe differs from its authored manifest")
    restore = (
        "instruction.md",
        "solution/solve.sh",
        "tests/test_contract.py",
        "contract.json",
        "tests/source_origin_probe.py",
    )
    context = {
        "task_digest": parent_task_digest,
        "task_revision": int(prior[1]),
        "source_digest": canonical_digest(source),
        "construction_digest": canonical_digest(previous),
        "artifact": manifest.model_dump(mode="json"),
        "design": design.model_dump(mode="json"),
        "public_instruction": files["instruction.md"].decode(),
        "restored_files": list(restore),
        "status": "Unvalidated prior revision supplied for repair, not acceptance evidence. Preserve or correct the authored files and contract using every required feedback item; run the checks and submit a complete new Construction. Controller-owned wrapper files are regenerated.",
    }
    return {
        "context": context,
        "files": [(name, files[name]) for name in restore],
        "design": design,
        "discovery": discovery,
    }


async def public_file_inventory(remote, source: dict) -> list[str]:
    """Read names from the pinned base tree, never from an author's mutable worktree."""
    path = "/private/tasksmith-base-file-inventory"
    command = (
        "git -C /workspace/repo ls-tree -r -z --name-only "
        + shlex.quote(source["base_sha"])
        + " > "
        + shlex.quote(path)
    )
    result = json.loads(await remote.shell(command, 30))
    if result.get("exit_code") != 0:
        raise RuntimeError("Could not capture the pinned base file inventory")
    raw = await remote.read(path)
    if not raw or not raw.endswith(b"\0"):
        raise ValueError("Incomplete base file inventory")
    names = raw[:-1].decode().split("\0")
    if len(set(names)) != len(names):
        raise ValueError("Invalid public file inventory")
    for name in names:
        safe_relative(name)
    return sorted(names)


async def construct(
    config: TasksmithConfig,
    source: dict,
    discovery: dict,
    root: Path,
    cache_root: Path,
    deadline: float,
    repair: dict | None = None,
    *,
    prior_construction: dict | None = None,
    parent_task_digest: str | None = None,
) -> dict:
    previous = None
    if prior_construction is not None:
        if not parent_task_digest or not repair or repair.get("repairable") is not True:
            raise ValueError("Semantic repair needs its parent digest and required repair feedback")
        previous = load_prior_construction(root, source, prior_construction, parent_task_digest)
    elif parent_task_digest is not None:
        raise ValueError("Semantic repair parent digest requires the prior construction")
    recovery = load_recovery(root, source)
    result_path = root / "construction.json"
    if result_path.exists():
        result = json.loads(result_path.read_text())
        if result.get("recovery_receipt_digest") != (
            recovery["receipt_digest"] if recovery else None
        ):
            raise ValueError("Retained construction recovery receipt changed")
        if result.get("prior_construction_digest") != (
            canonical_digest(prior_construction) if previous else None
        ):
            raise ValueError("Retained semantic repair parent changed")
        return result
    root.mkdir(parents=True, exist_ok=True)
    discovered = Discovery.model_validate(discovery["artifact"])
    if previous:
        discovered = previous["discovery"]
    if recovery and "bootstrap_discovery" in recovery["receipt"]:
        discovered = Discovery.model_validate(recovery["receipt"]["bootstrap_discovery"])
    budget = config.budget(source["id"])
    async with bootstrap_ready(
        config, source, discovered, root, cache_root, deadline, budget
    ) as ready:
        discovered, wc, readiness, remote = ready
        recipe = discovered.dependency_dockerfile
        stage_inputs = {
            "source": source,
            "discovery": discovered.model_dump(mode="json"),
            "bootstrap": readiness,
            "repair": repair,
        }
        if previous:
            stage_inputs["prior_revision"] = previous["context"]
            for name, data in previous["files"]:
                await remote.write("/output/task/" + name, data)
        if recovery:
            stage_inputs["construction_recovery"] = {
                "receipt_digest": recovery["receipt_digest"],
                "original_stage_root": recovery["receipt"]["original_stage_root"],
                "restored_files": [name for name, _ in recovery["files"]],
                "last_submission": recovery["receipt"]["last_submission"],
                "reason": recovery["receipt"]["reason"],
                "status": "Unvalidated partial author output. Reuse the retained design, fix the submitted metadata and public instruction, and run the checks. Previous files/submissions are not accepted evidence; submit a complete corrected Construction through the normal validator.",
            }
            for name, data in recovery["files"]:
                await remote.write("/output/task/" + name, data)
            design = recovery["design"]
        elif (
            previous
            and repair.get("stage") in {"execution", "construction"}
            and repair.get("status") == "needs_repair"
            and not repair.get("review")
            and not repair.get("scope_change_required")
        ):
            # A concrete control failure preserves the selected problem. The
            # author can correct its implementation/fixtures without paying for
            # an unchanged proposal; semantic review feedback still revisits scope.
            design = previous["design"]
        else:
            design = await artifact_stage(
                schema=Design,
                stage="design",
                inputs=stage_inputs,
                system=PROPOSE,
                prompt=json.dumps(stage_inputs),
                root=root / "design",
                budget=budget,
                model=config.author_model,
                runtime=config.author_runtime,
                max_cost=config.author_stage_limit_usd,
                max_turns=config.author_turns,
                deadline=wc.deadline,
                shell=remote.shell,
            )

        visible_files = await public_file_inventory(remote, source)
        specification = await approve_instruction(
            config=config,
            budget=budget,
            root=root / "public-specification",
            deadline=wc.deadline,
            initial_text=(
                previous["context"]["public_instruction"]
                if previous and design == previous["design"]
                else design.selected.task_request
            ),
            visible_files=visible_files,
        )
        instruction = specification["instruction"]
        await remote.write("/output/task/instruction.md", instruction.encode())
        stage_inputs["approved_public_instruction"] = instruction
        author_design = design.model_copy(deep=True)
        author_design.selected.task_request = instruction

        async def validate(value: Construction):
            payload = {}
            for file in ("instruction.md", "solution/solve.sh", "tests/test_contract.py"):
                payload[file] = (await remote.read("/output/task/" + file)).decode()
            if payload["instruction.md"] != instruction:
                await remote.write("/output/task/instruction.md", instruction.encode())
                raise ValueError(
                    "The independently approved public instruction is frozen during construction. "
                    "Its exact bytes have been restored. Align the verifier and metadata with "
                    "approved_public_instruction; request a later scope revision if it cannot be implemented."
                )
            # Static syntax/schema checks only. Generated code is never imported locally.
            with tempfile.TemporaryDirectory(prefix="tasksmith-static-") as temporary:
                emit_task(
                    Path(temporary) / "task",
                    source,
                    recipe,
                    execution_contract=value.contract,
                    instruction=payload["instruction.md"],
                    solution_script=payload["solution/solve.sh"],
                    protected_tests=payload["tests/test_contract.py"],
                    source_origin_probe=value.source_origin_probe,
                )
            save_json(root / "payload.json", payload)

        construction = await artifact_stage(
            schema=Construction,
            stage="construction",
            inputs={**stage_inputs, "design": author_design.model_dump(mode="json")},
            system=CONSTRUCT,
            prompt=json.dumps(
                {
                    **stage_inputs,
                    "selected": author_design.selected.model_dump(mode="json"),
                    "bootstrap_note": "Reference readiness passed on this exact dependency recipe. Preserve these installed dependencies; any further dependency change requires another construction revision and fresh readiness. Shell-only installs will not exist in Harbor.",
                }
            ),
            root=root / "author",
            budget=budget,
            model=config.author_model,
            runtime=config.author_runtime,
            max_cost=config.author_stage_limit_usd,
            max_turns=config.author_turns,
            deadline=wc.deadline,
            shell=remote.shell,
            validate=validate,
        )
        payload = json.loads((root / "payload.json").read_text())
        task_path = root / "task"
        if task_path.exists():
            from repo2rlenv.curation.artifacts import digest_task

            receipt = json.loads((root / "emitter.json").read_text())
            if digest_task(task_path) != receipt["task_digest"]:
                raise ValueError("Retained package changed after emission")
        else:
            receipt = emit_task(
                task_path,
                source,
                recipe,
                execution_contract=construction.contract,
                instruction=payload["instruction.md"],
                solution_script=payload["solution/solve.sh"],
                protected_tests=payload["tests/test_contract.py"],
                source_origin_probe=construction.source_origin_probe,
            )
            save_json(root / "emitter.json", receipt)
        result = {
            "artifact": construction.model_dump(mode="json"),
            "design": design.model_dump(mode="json"),
            "emitter": receipt,
            "task_path": str(task_path),
            "readiness": readiness,
            "bootstrap_discovery": discovered.model_dump(mode="json"),
            "source_receipt": discovery["source_receipt"],
            "public_instruction": payload["instruction.md"],
            "specification_review": specification,
            "recovery_receipt_digest": recovery["receipt_digest"] if recovery else None,
            "prior_construction_digest": canonical_digest(prior_construction) if previous else None,
        }
        save_json(result_path, result)
    return result
