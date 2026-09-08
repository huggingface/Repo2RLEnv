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

from repo2rlenv.tasksmith import cpu_fixture
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
from repo2rlenv.tasksmith.readiness_repair import (
    SmokeDependencyFailure,
    correct_generated_smoke,
    generated_smoke_failure,
)
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
    flags = {"-q", "-v", "-vv", "-s", "-x", "--disable-warnings", "--strict-markers", "--no-header"}
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
    selection = False
    for row in readiness.get("checks", []):
        if row.get("command") not in discovered.upstream_test_commands:
            continue
        output = str(row.get("stdout", "")) + "\n" + str(row.get("stderr", ""))
        # Pytest also reports "found no collectors" after an import fails.
        # Collection/dependency errors cannot authorize changing test selectors.
        if row.get("timed_out") or re.search(
            r"(?i)(\b(?:ModuleNotFoundError|ImportError|SyntaxError|ConftestImportFailure|INTERNALERROR)\b|ERROR collecting|while importing test module|Traceback \(most recent call last\))",
            output,
        ):
            return False
        if row.get("exit_code") not in {4, 5}:
            continue
        if re.search(
            r"(?i)(ERROR:\s*(?:not found|file or directory not found)|no tests (?:ran|collected)|collected 0 items)",
            output,
        ):
            selection = True
    return selection


def _profile_failure(discovered, readiness):
    """Recognize actual GPU capability exceptions in the supported CPU-only profile.

    A mention in source, a warning, or a successful expected-exception test is not
    sufficient. This identifies the failing invocation, not PR unsuitability.
    """
    if readiness.get("passed"):
        return None
    commands = set(discovered.readiness_commands + discovered.upstream_test_commands)
    for index, row in enumerate(readiness.get("checks", [])):
        if row.get("command") not in commands or not row.get("exit_code") or row.get("timed_out"):
            continue
        output = str(row.get("stdout", "")) + "\n" + str(row.get("stderr", ""))
        match = re.search(
            r"(?m)^(?:E\s+)?(?:ValueError|RuntimeError|AssertionError): "
            r"(?:Your setup doesn't support bf16/gpu\.|Torch not compiled with CUDA enabled|"
            r"Found no NVIDIA driver on your system|No CUDA GPUs are available)[^\n]*$",
            output,
        )
        if match:
            return {
                "profile": "cpu-direct",
                "check_index": index,
                "command": row["command"],
                "exception": match.group(0),
            }
    return None


def _require_compatible_profile(source, discovered, readiness, root):
    failure = _profile_failure(discovered, readiness)
    if failure is None:
        return
    path = root / "profile-mismatch.json"
    save_json(
        path,
        {
            "classification": "profile_configuration_mismatch",
            "source": {key: source[key] for key in ("base_sha", "head_sha")},
            "readiness_digest": canonical_digest(readiness),
            **failure,
        },
    )
    raise BootstrapReadinessError(
        "CPU profile/configuration mismatch: "
        + failure["exception"]
        + ". The selected invocation requires a supported CPU configuration or a different "
        "resource profile; dependency pin changes or weaker tests do not resolve this evidence. "
        "Keep the failed checks and obtain an explicit profile/fixture decision; inspect "
        + str(path)
    )


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


async def _pytest_observation(shell, source, command, *, collect_only, read=None, cpu_context=None):
    if read is None:
        raise BootstrapReadinessError("Pytest observation needs bounded remote file transfer")
    args, _ = _pytest_command(command)
    if collect_only:
        args = [*args, "--collect-only"]
    cpu_runtime = cpu_fixture.approved_runtime(**cpu_context) if cpu_context else None
    inputs = {"source": source, "args": args, "capture_policy": 2}
    if cpu_runtime is not None:
        inputs.update(capture_policy=3, cpu_fixture=cpu_runtime)
    identity = canonical_digest(inputs)
    prefix = "/private/tasksmith-pytest-observations/" + canonical_digest(
        {"input_digest": identity, "created_ns": time.time_ns()}
    )
    capture = {
        "args": args,
        "input_digest": identity,
        "limit": 128_000,
        "data_path": prefix + ".json",
        "receipt_path": prefix + ".receipt.json",
    }
    if cpu_runtime is not None:
        capture["cpu_fixture"] = cpu_runtime
    # Trusted observation wrapper runs remotely. No target/test imports occur on the controller.
    script = """# tasksmith-pytest-observation-capture
import contextlib,hashlib,io,json,pathlib,sys,pytest
arg=json.loads(sys.argv[1])
class Observe:
    def __init__(self): self.collected=[]; self.executed=[]
    def pytest_collection_finish(self,session): self.collected=[item.nodeid for item in session.items]
    def pytest_runtest_logreport(self,report):
        if report.when == 'call' and not report.skipped: self.executed.append(report.nodeid)
p=Observe(); out=io.StringIO(); err=io.StringIO()
with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
    code=int(pytest.main(arg['args'],plugins=[p]))
report={'exit_code':code,'collected':p.collected,'executed':p.executed,'stdout':out.getvalue()[-12000:],'stderr':err.getvalue()[-12000:]}
data=json.dumps(report,allow_nan=False).encode()
path=pathlib.Path(arg['receipt_path']); path.parent.mkdir(parents=True,exist_ok=True)
receipt={'input_digest':arg['input_digest'],'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'status':'present' if len(data)<=arg['limit'] else 'oversized'}
if len(data)<=arg['limit']: pathlib.Path(arg['data_path']).write_bytes(data)
path.write_text(json.dumps(receipt,allow_nan=False))
sys.exit(code)
"""
    if cpu_runtime is not None:
        # The same fixed constructor context surrounds collection AND execution.
        # Its source checks and receipt are included in the complete hashed report.
        script = (
            cpu_fixture.runtime_source()
            + "\n"
            + script.replace(
                "    code=int(pytest.main(arg['args'],plugins=[p]))",
                "    code,cpu_report=_cpu_observe(arg['cpu_fixture'],arg['args'],lambda args: pytest.main(args,plugins=[p]))",
            ).replace(
                "data=json.dumps(report,allow_nan=False).encode()",
                "report['cpu_fixture']=cpu_report\ndata=json.dumps(report,allow_nan=False).encode()",
            )
        )
    observed = json.loads(
        await shell(
            "cd /workspace/repo && git reset --hard "
            + shlex.quote(source["head_sha"])
            + " && git clean -fd && python "
            + ("-I " if cpu_runtime else "")
            + "-c "
            + shlex.quote(script)
            + " "
            + shlex.quote(json.dumps(capture)),
            300,
        )
    )
    if observed.get("timed_out"):
        raise BootstrapReadinessError("Pytest observation timed out; execution is unproven")
    try:
        raw_receipt = await read(capture["receipt_path"])
    except (OSError, ValueError) as exc:
        raise BootstrapReadinessError(
            "Pytest observation report missing; collection/execution is unproven"
        ) from exc
    if len(raw_receipt) > 16_000:
        raise BootstrapReadinessError("Pytest observation transfer receipt exceeds bound")
    receipt = json.loads(raw_receipt)
    if receipt.get("input_digest") != identity:
        raise BootstrapReadinessError("Pytest observation transfer identity differs")
    if (
        receipt.get("status") != "present"
        or type(receipt.get("size")) is not int
        or not 0 <= receipt["size"] <= capture["limit"]
    ):
        raise BootstrapReadinessError("Pytest observation exceeds the 128 KB transfer bound")
    data = await read(capture["data_path"])
    if len(data) != receipt["size"] or hashlib.sha256(data).hexdigest() != receipt.get("sha256"):
        raise BootstrapReadinessError("Pytest observation transfer size or SHA256 differs")
    report = json.loads(data)
    if cpu_runtime is not None:
        save_json(
            cpu_context["root"].parent / "observations" / (prefix.rsplit("/", 1)[-1] + ".json"),
            {
                "input_digest": identity,
                "capture": capture,
                "transport": observed,
                "receipt": receipt,
                "report": report,
            },
        )
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
    if cpu_runtime is not None:
        cpu_report = report.get("cpu_fixture", {})
        if (
            cpu_report.get("input_digest") != cpu_runtime["input_digest"]
            or cpu_report.get("argv") != args
            or cpu_report.get("source_unchanged") is not True
            or cpu_report.get("refused")
            or cpu_report.get("error")
            or cpu_report.get("source_error")
            or cpu_report.get("exit_code") != report["exit_code"]
        ):
            raise BootstrapReadinessError(
                "CPU fixture observation unsupported or source preservation unproven: "
                + json.dumps(cpu_report)
            )
    return {**report, "transfer": {"size": receipt["size"], "sha256": receipt["sha256"]}}


async def _pinned_test_source(shell, read, commit, module, label, limit):
    """Capture a regular pinned Git blob without transporting code in clipped stdout."""
    key = canonical_digest({"commit": commit, "module": module})
    prefix = "/private/tasksmith-selector-sources/" + key
    args = {
        "commit": commit,
        "module": module,
        "limit": limit,
        "data_path": prefix + ".source",
        "receipt_path": prefix + ".json",
    }
    script = r"""# tasksmith-selector-source-capture
import hashlib,json,pathlib,subprocess,sys
arg=json.loads(sys.argv[1]); receipt={"commit":arg["commit"],"module":arg["module"]}
path=pathlib.Path(arg["receipt_path"]); path.parent.mkdir(parents=True,exist_ok=True)
data_path=pathlib.Path(arg["data_path"]); data_path.unlink(missing_ok=True)
def git(*words):
    return subprocess.run(["git","-C","/workspace/repo",*words],capture_output=True)
try:
    commit=git("cat-file","-e",arg["commit"]+"^{commit}")
    if commit.returncode: raise ValueError("Pinned commit unavailable")
    tree=git("ls-tree","-z",arg["commit"],"--",arg["module"])
    if tree.returncode: raise ValueError("Pinned tree lookup failed")
    if not tree.stdout:
        receipt.update(status="missing",size=0,sha256=hashlib.sha256(b"").hexdigest())
    else:
        rows=tree.stdout.split(b"\0")
        if len(rows)!=2 or rows[1]: raise ValueError("Ambiguous pinned tree entry")
        metadata,name=rows[0].split(b"\t",1); mode,kind,oid=metadata.decode().split()
        if name.decode()!=arg["module"] or mode not in ("100644","100755") or kind!="blob":
            raise ValueError("Pinned test module is not a regular file")
        size=git("cat-file","-s",oid)
        if size.returncode: raise ValueError("Pinned blob size unavailable")
        count=int(size.stdout)
        if count<0 or count>arg["limit"]: raise ValueError("Pinned test source exceeds transfer bound")
        blob=git("cat-file","blob",oid)
        if blob.returncode or len(blob.stdout)!=count: raise ValueError("Pinned blob capture incomplete")
        data_path.write_bytes(blob.stdout)
        receipt.update(status="present",size=count,sha256=hashlib.sha256(blob.stdout).hexdigest(),git_blob_id=oid)
except Exception as exc:
    receipt.update(status="error",reason=type(exc).__name__+": "+str(exc))
path.write_text(json.dumps(receipt,allow_nan=False))
"""
    observed = json.loads(
        await shell(
            "python -I -c " + shlex.quote(script) + " " + shlex.quote(json.dumps(args)), 120
        )
    )
    if observed.get("exit_code") != 0:
        raise BootstrapReadinessError(f"Pinned source capture failed: {module} ({label})")
    raw_receipt = await read(args["receipt_path"])
    if len(raw_receipt) > 16_000:
        raise BootstrapReadinessError("Pinned source transfer receipt exceeds bound")
    receipt = json.loads(raw_receipt)
    if receipt.get("commit") != commit or receipt.get("module") != module:
        raise BootstrapReadinessError("Pinned source transfer receipt identity differs")
    if receipt.get("status") == "error":
        raise BootstrapReadinessError(str(receipt.get("reason", "Pinned source capture failed")))
    if receipt.get("status") == "missing":
        if label == "head":
            raise BootstrapReadinessError(f"Pinned upstream module is unavailable: {module}")
        data = b""  # Only an explicitly absent base-tree entry represents a new test file.
    elif receipt.get("status") == "present":
        if type(receipt.get("size")) is not int or not 0 <= receipt["size"] <= limit:
            raise BootstrapReadinessError(
                "Pinned source exceeds the remaining 128 KB transfer bound"
            )
        data = await read(args["data_path"])
    else:
        raise BootstrapReadinessError("Pinned source transfer status is incomplete")
    if (
        len(data) > limit
        or len(data) != receipt.get("size")
        or hashlib.sha256(data).hexdigest() != receipt.get("sha256")
    ):
        raise BootstrapReadinessError("Pinned source transfer size or SHA256 differs")
    return data.decode(), receipt


async def _selector_sources(shell, source, discovered, root, *, read=None, cpu_context=None):
    path = root / "source-and-collection.json"
    modules = sorted(
        {
            module
            for command in discovered.upstream_test_commands
            for module in _pytest_command(command)[1]
        }
    )
    inputs = {"source": source, "modules": modules, "capture_policy": 2}
    if cpu_context:
        inputs["cpu_fixture"] = cpu_fixture.approved_runtime(**cpu_context)
    identity = canonical_digest(inputs)
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
    if shell is None or read is None:
        raise BootstrapReadinessError(
            "Selector correction needs remote source and collection observations"
        )
    if len(modules) > 8:
        raise BootstrapReadinessError("Selector repair exceeds the bounded eight-module inspection")
    files, transfers, changed, total = {}, {}, set(), 0
    source_limit = 2 * cpu_fixture.MAX_INPUT_BYTES if cpu_context else 128_000
    for module in modules:
        versions, receipts = {}, {}
        for label, commit in (("before", source["base_sha"]), ("head", source["head_sha"])):
            versions[label], receipts[label] = await _pinned_test_source(
                shell, read, commit, module, label, source_limit - total
            )
            total += len(versions[label].encode())
        old, new = (_test_definitions(versions[label], module) for label in ("before", "head"))
        changed.update(name for name, value in new.items() if old.get(name) != value)
        files[module] = versions
        transfers[module] = receipts
    collection = await _pytest_observation(
        shell,
        source,
        "python -m pytest " + shlex.join(modules) + " -q",
        collect_only=True,
        read=read,
        cpu_context=cpu_context,
    )
    available = {node.split("[", 1)[0] for node in collection["collected"]}
    evidence = {
        "input_digest": identity,
        "modules": modules,
        "source_files": files,
        "source_transfers": transfers,
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


async def _selector_collection(
    shell, source, corrected, evidence, root, *, read=None, cpu_context=None
):
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
        report = await _pytest_observation(
            shell, source, command, collect_only=True, read=read, cpu_context=cpu_context
        )
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


def _readiness_inputs(
    source, discovered, key, selector_proof=None, smoke_proof=None, cpu_proof=None
):
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
    if smoke_proof is not None:
        inputs["smoke_proof"] = smoke_proof
    if cpu_proof is not None:
        inputs["cpu_fixture_proof"] = cpu_proof
    return inputs


async def _reference_readiness(
    remote,
    source,
    discovered,
    root,
    *,
    key,
    cached,
    selector_proof=None,
    smoke_proof=None,
    cpu_context=None,
    cpu_proof=None,
):
    if (cpu_context is None) != (cpu_proof is None) or (cpu_context and selector_proof is None):
        raise BootstrapReadinessError("CPU readiness requires approval and full coverage proof")
    if (
        cpu_context
        and cpu_fixture.approved_runtime(**cpu_context)["input_digest"] != cpu_proof["input_digest"]
    ):
        raise BootstrapReadinessError("CPU approval differs from readiness proof")
    inputs = _readiness_inputs(source, discovered, key, selector_proof, smoke_proof, cpu_proof)
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
            observed = await _pytest_observation(
                remote.shell,
                source,
                command,
                collect_only=False,
                read=remote.read,
                cpu_context=cpu_context,
            )
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
    if smoke_proof is not None:
        readiness["smoke_proof"] = smoke_proof
    if cpu_proof is not None:
        readiness["cpu_fixture_proof"] = cpu_proof
    save_json(path, readiness)
    return readiness


async def _cpu_fixture_readiness(
    remote,
    source,
    discovered,
    failed,
    root,
    *,
    config,
    budget,
    deadline,
    key,
    cached,
    selector_proof=None,
    smoke_proof=None,
):
    """One reviewed resource choice, followed by unchanged full reference checks.

    No image is built here. Any error remains terminal for this bootstrap phase;
    partial/rejected approvals cannot authorize another dependency attempt.
    """
    if root.exists():
        raise BootstrapReadinessError(
            "Retained CPU fixture attempt requires reconciliation; no adaptation reroll"
        )
    if source.get("repo") != "huggingface/trl":
        raise BootstrapReadinessError("CPU fixture adapter only supports reviewed TRL configs")
    root.mkdir(parents=True)
    save_json(
        root / "attempt.json",
        {
            "source": source,
            "discovery": discovered.model_dump(mode="json"),
            "failure": failed,
            "deadline": deadline,
            "cache_key": key,
        },
    )

    async def shell(command, timeout_sec=120):
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeoutError("Original bootstrap deadline exhausted")
        return await remote.shell(command, min(timeout_sec, remaining))

    # This tiny stdlib-only observation is parsed as a complete bounded JSON
    # object, never inferred from requested pins or a default SDK version. Keep
    # even failed output before refusing unsupported metadata, before model cost.
    raw_version = await shell(cpu_fixture.VERSION_COMMAND, 30)
    save_json(
        root / "installed-transformers.json",
        {"command": cpu_fixture.VERSION_COMMAND, "response": raw_version},
    )
    observed_version = cpu_fixture.InstalledVersion(
        command=cpu_fixture.VERSION_COMMAND,
        response=raw_version,
        sha256=hashlib.sha256(raw_version.encode()).hexdigest(),
    )
    files, transfers, constructors = {}, {}, set()
    modules = sorted(
        {
            module
            for command in discovered.upstream_test_commands
            for module in _pytest_command(command)[1]
        }
    )
    if not modules or len(modules) > 8:
        raise BootstrapReadinessError("CPU fixture inspection requires 1–8 complete test modules")
    pending = [(module, True) for module in modules]
    # Pytest's implicit local fixtures are review inputs too. Missing files have
    # explicit pinned-tree absence receipts, never inferred from clipped stdout.
    for module in modules:
        pending.extend(
            (str(parent / "conftest.py"), False) for parent in PurePosixPath(module).parents
        )
        pending.extend(
            (str(parent / "__init__.py"), False)
            for parent in PurePosixPath(module).parents
            if str(parent) != "."
        )
    seen = set()
    while pending:
        path, required = pending.pop(0)
        if path in seen:
            continue
        seen.add(path)
        safe_relative(path)
        if len(seen) > 32:
            raise BootstrapReadinessError("CPU fixture support-source inspection exceeds bound")
        text, receipt = await _pinned_test_source(
            shell,
            remote.read,
            source["head_sha"],
            path,
            "head" if required else "support",
            192_000,
        )
        transfers[path] = receipt
        save_json(root / "source-transfers.json", transfers)
        if receipt["status"] == "missing":
            continue
        files[path] = text
        save_json(root / "captured-sources.json", files)
        if (
            len(files) > 24
            or sum(len(text.encode()) for text in files.values()) > cpu_fixture.MAX_INPUT_BYTES
        ):
            raise BootstrapReadinessError("Complete CPU fixture source exceeds approval bounds")
        tree = ast.parse(text)
        constructors.update(
            name
            for node in ast.walk(tree)
            if (
                name := (
                    node.id
                    if isinstance(node, ast.Name)
                    else node.attr
                    if isinstance(node, ast.Attribute)
                    else node.name
                    if isinstance(node, ast.alias)
                    else None
                )
            )
            in cpu_fixture.CONFIG_MODULES
        )
        # Capture local test helper imports; do not import target/test code on the
        # controller or recursively collect the entire third-party dependency tree.
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names if alias.name.startswith("tests.")]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    parents = PurePosixPath(path).parent.parts
                    if node.level <= len(parents):
                        prefix = ".".join(parents[: len(parents) - node.level + 1])
                        names = (
                            [prefix + "." + node.module]
                            if node.module
                            else [
                                prefix + "." + alias.name
                                for alias in node.names
                                if alias.name != "*"
                            ]
                        )
                elif node.module and (node.module == "tests" or node.module.startswith("tests.")):
                    names = [node.module]
                    if node.module == "tests":
                        names.extend(
                            "tests." + alias.name for alias in node.names if alias.name != "*"
                        )
            for name in names:
                if name == "tests" or name.startswith("tests."):
                    pending.extend(
                        [
                            (name.replace(".", "/") + ".py", False),
                            (name.replace(".", "/") + "/__init__.py", False),
                        ]
                    )
    if not constructors:
        raise BootstrapReadinessError("No source-supported allowlisted TRL constructor found")
    for path in sorted(
        {
            "trl/trainer/base_config.py",
            *(cpu_fixture.CONFIG_MODULES[name].replace(".", "/") + ".py" for name in constructors),
        }
    ):
        text, receipt = await _pinned_test_source(
            shell,
            remote.read,
            source["head_sha"],
            path,
            "head",
            192_000,
        )
        files[path], transfers[path] = text, receipt
    save_json(root / "source-transfers.json", transfers)
    save_json(root / "captured-sources.json", files)
    request = cpu_fixture.CpuFixtureRequest(
        source=cpu_fixture.PRIdentity.from_url(source["url"]),
        base_sha=source["base_sha"],
        head_sha=source["head_sha"],
        discovery=discovered,
        transformers_version=observed_version.version,
        version_observation=observed_version,
        constructors=sorted(constructors),
        readiness=failed,
        source_files=[
            cpu_fixture.CapturedSource(
                path=path,
                text=text,
                size=len(text.encode()),
                sha256=hashlib.sha256(text.encode()).hexdigest(),
            )
            for path, text in sorted(files.items())
        ],
    )
    context = {
        "request": request,
        "config": config,
        "budget": budget,
        "deadline": deadline,
        "root": root / "approval",
    }
    await cpu_fixture.review_cpu_fixture(**context)
    proof_path = context["root"] / "result.json"
    raw_proof = proof_path.read_bytes()
    result = json.loads(raw_proof)
    proof = {
        "result_path": str(proof_path),
        "sha256": hashlib.sha256(raw_proof).hexdigest(),
        "input_digest": result["input_digest"],
        "assessment": result["assessment"],
    }
    evidence = await _selector_sources(
        shell,
        source,
        discovered,
        root / "coverage",
        read=remote.read,
        cpu_context=context,
    )
    if selector_proof:
        evidence["required_changed_anchors"] = sorted(
            set(evidence["required_changed_anchors"])
            | set(selector_proof["required_changed_anchors"])
        )
    coverage = await _selector_collection(
        shell,
        source,
        discovered,
        evidence,
        root / "coverage",
        read=remote.read,
        cpu_context=context,
    )
    readiness = await _reference_readiness(
        remote,
        source,
        discovered,
        root,
        key=key,
        cached=cached,
        selector_proof=coverage,
        smoke_proof=smoke_proof,
        cpu_context=context,
        cpu_proof=proof,
    )
    if not readiness["passed"]:
        raise BootstrapReadinessError(
            "Approved CPU fixture still failed complete unchanged readiness; no additional "
            "dependency attempt or adaptation is authorized; inspect "
            + str(root / "readiness.json")
        )
    return readiness


async def _dependency_collection(shell, read, source, corrected, root, deadline):
    """Prove the selected modules import and collect before the final image rebuild."""
    modules = sorted(
        {
            module
            for command in corrected.upstream_test_commands
            for module in _pytest_command(command)[1]
        }
    )
    if not modules:
        return  # Explicit no-relevant-tests route still needs all original readiness checks.
    if len(modules) > 8:
        raise ValueError("Dependency correction exceeds bounded eight-module collection")
    identity = canonical_digest(
        {
            "policy": 1,
            "source": source,
            "discovery": corrected.model_dump(mode="json"),
            "modules": modules,
        }
    )
    directory = root / "dependency-collection" / identity
    proof_path = directory / "proof.json"
    if proof_path.exists():
        proof = json.loads(proof_path.read_text())
        if (
            proof.get("input_digest") != identity
            or proof.get("modules") != modules
            or not proof.get("passed")
            or proof.get("source_check", {}).get("exit_code") != 0
            or proof.get("source_after", {}).get("exit_code") != 0
            or proof.get("reset", {}).get("exit_code") != 0
            or len(proof.get("reports", [])) != len(modules)
            or any(
                report.get("exit_code") != 0
                or not report.get("collected")
                or report.get("module") != module
                for module, report in zip(modules, proof["reports"], strict=True)
            )
        ):
            raise BootstrapReadinessError("Retained dependency collection proof is incomplete")
        return
    if shell is None or read is None:
        raise BootstrapReadinessError(
            "Dependency correction lacks complete retained module collection; reconcile before rebuilding"
        )

    async def bounded_shell(command, timeout_sec=120):
        remaining = int(deadline - time.time())
        if remaining <= 0:
            raise TimeoutError("Original dependency diagnosis deadline exhausted")
        return await shell(command, min(timeout_sec, remaining))

    # Installation may alter the dependency environment, never tracked source,
    # tests, or a local test plugin. Require a clean original base/head checkout.
    script = """# tasksmith-dependency-source-check
import json,subprocess,sys
allowed=json.loads(sys.argv[1])
def git(*args): return subprocess.run(['git','-C','/workspace/repo',*args],capture_output=True)
head=git('rev-parse','HEAD'); status=git('status','--porcelain','--untracked-files=all')
sys.exit(0 if head.returncode==status.returncode==0 and head.stdout.decode().strip() in allowed and not status.stdout else 42)
"""
    source_command = (
        "python -I -c "
        + shlex.quote(script)
        + " "
        + shlex.quote(json.dumps([source["base_sha"], source["head_sha"]]))
    )
    source_check = json.loads(await bounded_shell(source_command))
    directory.mkdir(parents=True, exist_ok=True)
    attempt_path = directory / f"attempt-{len(list(directory.glob('attempt-*.json')))}.json"
    proof = {
        "input_digest": identity,
        "modules": modules,
        "source_check": source_check,
        "reports": [],
        "passed": False,
        "fresh_image_required": True,
    }
    save_json(attempt_path, proof)
    if source_check.get("exit_code") != 0 or source_check.get("timed_out"):
        raise ValueError(
            "Dependency diagnosis changed source/tests or left untracked repository files; "
            "restore the pinned checkout before collecting. Inspect " + str(attempt_path)
        )
    try:
        for module in modules:
            report = await _pytest_observation(
                bounded_shell,
                source,
                "python -m pytest " + shlex.quote(module) + " -q",
                collect_only=True,
                read=read,
            )
            proof["reports"].append({"module": module, **report})
            save_json(attempt_path, proof)
            _require_compatible_profile(
                source,
                corrected.model_copy(update={"upstream_test_commands": ["pytest " + module]}),
                {"passed": False, "checks": [{"command": "pytest " + module, **report}]},
                directory,
            )
            if report["exit_code"] or not any(
                node.startswith(module + "::") for node in report["collected"]
            ):
                raise ValueError(
                    "Dependency correction needs successful full-module collection for every "
                    "selected module. Diagnose remaining imports/test extras in this workspace, "
                    "install diagnostic pins and resubmit the complete recipe; do not edit source "
                    "or remove checks. Failed module "
                    + module
                    + "; inspect "
                    + str(attempt_path)
                    + "\nObserved collection:\n"
                    + (str(report.get("stdout", "")) + "\n" + str(report.get("stderr", "")))[-6000:]
                )
        proof["source_after"] = json.loads(await bounded_shell(source_command))
        save_json(attempt_path, proof)
        if proof["source_after"].get("exit_code") or proof["source_after"].get("timed_out"):
            raise ValueError(
                "Module collection changed the pinned source/tests; collection proof rejected"
            )
    finally:
        proof["reset"] = json.loads(
            await bounded_shell(
                "cd /workspace/repo && git reset --hard "
                + shlex.quote(source["base_sha"])
                + " && git clean -fd",
                120,
            )
        )
        save_json(attempt_path, proof)
        if proof["reset"].get("exit_code") or proof["reset"].get("timed_out"):
            raise BootstrapReadinessError(
                "Dependency collection could not restore the pinned source"
            )
    proof["passed"] = True
    save_json(attempt_path, proof)
    save_json(proof_path, proof)


async def _correct_discovery(
    config,
    source,
    discovered,
    readiness,
    root,
    *,
    budget,
    deadline,
    shell,
    selector=False,
    read=None,
):
    original = discovered.model_dump(mode="json")
    inputs = {"source": source, "discovery": original, "failed_readiness": readiness}
    stage = "bootstrap-selector-repair-1" if selector else "bootstrap-repair-1"
    _require_compatible_profile(source, discovered, readiness, root)
    if selector:
        if not _selector_failure(discovered, readiness):
            raise BootstrapReadinessError(
                "Selector repair requires explicit pytest selection failure"
            )
        evidence = await _selector_sources(shell, source, discovered, root / stage, read=read)
        inputs["upstream_source_and_collection"] = evidence
    else:
        inputs["dependency_collection_policy"] = 1

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
            await _selector_collection(shell, source, value, evidence, root / stage, read=read)
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
        await _dependency_collection(shell, read, source, value, root / stage, deadline)

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
        else "\nRepair only the dependency bootstrap from the retained failure. Preserve the useful outcome, behaviors, exact readiness and upstream test commands. Inspect selected modules, conftest/test helpers and their transitive imports plus declared test extras. Diagnose the complete import/collection closure in this existing sandbox, not only the first missing package. You may install pinned dependencies diagnostically; preserve source and tests. Before committing, full selected modules must collect successfully at pinned HEAD; the host enforces this and returns remaining import failures. Record all required pins in the reproducible dependency-only Dockerfile, preserving its immutable base, and explain the evidence in dependency_inputs. GPU/configuration conflicts require an explicit profile decision, not dependency churn or weaker tests. A fresh second image must still pass every original readiness and upstream check; diagnostic collection alone is not reference success."
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
    """At most two builds, one smoke/selector correction and one CPU adaptation."""
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
    cpu_root = root / "bootstrap-cpu-fixture-1"
    if cpu_root.exists():
        raise BootstrapReadinessError(
            "Retained CPU fixture attempt requires reconciliation; cannot reopen or reroll"
        )
    cache = BootstrapCache(cache_root)
    selector_proof = None
    smoke_root = root / "bootstrap-smoke-repair-1"
    smoke_attempted = False
    smoke_proof = None
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
                _readiness_inputs(source, discovered, key, selector_proof, smoke_proof)
            ):
                raise BootstrapReadinessError("Retained failed readiness belongs to changed inputs")
            _require_compatible_profile(source, discovered, retained, attempt_root)
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
            if generated_smoke_failure(discovered, retained) is not None:
                if smoke_root.exists():
                    # Only a completed, independently classified dependency failure
                    # can recover the unchanged-command dependency correction below.
                    # The helper validates cached identity/evidence and never rerolls it.
                    if not (smoke_root / "phase.json").is_file():
                        raise BootstrapReadinessError(
                            "Unclaimed smoke evidence needs explicit workspace reconciliation"
                        )
                    smoke_phase = json.loads((smoke_root / "phase.json").read_text())
                    if (
                        smoke_phase.get("status") != "completed"
                        or not (smoke_root / "result.json").is_file()
                    ):
                        raise BootstrapReadinessError(
                            "Retained smoke diagnosis is incomplete; explicit reconciliation is required"
                        )

                    async def retained_source_read(*args, **kwargs):
                        raise BootstrapReadinessError(
                            "Retained smoke diagnosis cannot execute remote diagnostics"
                        )

                    try:
                        await correct_generated_smoke(
                            config,
                            source,
                            discovered,
                            retained,
                            smoke_root,
                            budget=budget,
                            deadline=bootstrap_deadline,
                            # Preserve the original diagnostics-enabled identity. The
                            # completed cache path never calls this refusing sentinel.
                            shell=retained_source_read,
                        )
                    except SmokeDependencyFailure:
                        smoke_attempted = True
                    else:
                        raise BootstrapReadinessError(
                            "Retained smoke correction needs explicit workspace reconciliation; "
                            "it cannot reopen the old workspace or reset the correction attempt"
                        )
                elif not (root / "bootstrap-repair-1/artifact.json").exists():
                    raise BootstrapReadinessError(
                        "Retained generated readiness failure needs explicit workspace reconciliation"
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
                    smoke_proof=smoke_proof,
                )
                mismatch = _profile_failure(discovered, readiness)
                if mismatch and (
                    source.get("repo") != "huggingface/trl"
                    or mismatch["command"] not in discovered.upstream_test_commands
                ):
                    _require_compatible_profile(source, discovered, readiness, attempt_root)
                if generated_smoke_failure(discovered, readiness) is not None:
                    if smoke_attempted:
                        raise BootstrapReadinessError(
                            "Reference readiness still failed after two bootstrap attempts; "
                            "the one generated-smoke diagnosis is exhausted; retain the failed checks"
                        )
                    smoke_attempted = True
                    try:
                        corrected = await correct_generated_smoke(
                            config,
                            source,
                            discovered,
                            readiness,
                            smoke_root,
                            budget=budget,
                            deadline=bootstrap_deadline,
                            shell=remote.shell,
                        )
                    except SmokeDependencyFailure:
                        # An invalid import may be an author mistake or a real dependency
                        # problem. Only the independent assessment selects this fallback.
                        pass
                    else:
                        discovered = corrected
                        proof_path = smoke_root / "result.json"
                        proof_bytes = proof_path.read_bytes()
                        proof = json.loads(proof_bytes)
                        smoke_proof = {
                            "result_path": str(proof_path),
                            "sha256": hashlib.sha256(proof_bytes).hexdigest(),
                            "input_digest": proof["input_digest"],
                            "assessment": proof["assessment"],
                        }
                        smoke_readiness_root = attempt_root / "generated-smoke-readiness-1"
                        readiness = await _reference_readiness(
                            remote,
                            source,
                            discovered,
                            smoke_readiness_root,
                            key=key,
                            cached=cached,
                            selector_proof=selector_proof,
                            smoke_proof=smoke_proof,
                        )
                        failed_path = smoke_readiness_root / "readiness.json"
                        mismatch = _profile_failure(discovered, readiness)
                        if mismatch and (
                            source.get("repo") != "huggingface/trl"
                            or mismatch["command"] not in discovered.upstream_test_commands
                        ):
                            _require_compatible_profile(
                                source, discovered, readiness, smoke_readiness_root
                            )
                        if generated_smoke_failure(discovered, readiness) is not None:
                            raise BootstrapReadinessError(
                                "Corrected generated smoke still failed full reference readiness; "
                                "inspect " + str(failed_path)
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
                        read=remote.read,
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
                        smoke_proof=smoke_proof,
                    )
                    failed_path = selection_root / "readiness.json"
                if _profile_failure(discovered, readiness):
                    # Retain the genuine failed invocation before considering the
                    # one fixed runtime-resource adaptation. No pin/selector edits.
                    try:
                        _require_compatible_profile(
                            source, discovered, readiness, failed_path.parent
                        )
                    except BootstrapReadinessError:
                        if source.get("repo") != "huggingface/trl":
                            raise
                    readiness = await _cpu_fixture_readiness(
                        remote,
                        source,
                        discovered,
                        readiness,
                        cpu_root,
                        config=config,
                        budget=budget,
                        deadline=bootstrap_deadline,
                        key=key,
                        cached=cached,
                        selector_proof=selector_proof,
                        smoke_proof=smoke_proof,
                    )
                    failed_path = cpu_root / "readiness.json"
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
                        read=remote.read,
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
