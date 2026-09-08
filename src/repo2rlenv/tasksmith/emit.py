"""Controller-owned, source-only Python Harbor packaging (no task code executes here)."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import tempfile
import tomllib
from pathlib import Path, PurePosixPath

import tomli_w

from repo2rlenv.curation.artifacts import digest_task, finalize, validate_dependency_pins
from repo2rlenv.curation.models import Contract

POLICY_VERSION = 1
APT_STANZA = (
    "RUN apt-get update && apt-get install -y --no-install-recommends "
    "git curl ca-certificates build-essential ripgrep && rm -rf /var/lib/apt/lists/*"
)


def _source(source: dict) -> dict:
    fields = {key: source[key] for key in ("id", "repo", "url", "base_sha", "head_sha")}
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", fields["repo"]):
        raise ValueError("Invalid repository")
    if any(part in {".", ".."} for part in fields["repo"].split("/")):
        raise ValueError("Invalid repository")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", fields["id"]):
        raise ValueError("Invalid source ID")
    for key in ("base_sha", "head_sha"):
        if not re.fullmatch(r"[0-9a-f]{40}", fields[key]):
            raise ValueError("Source commits must be immutable Git SHAs")
    if fields["base_sha"] == fields["head_sha"]:
        raise ValueError("Base and reference must differ")
    if not re.fullmatch(
        rf"https://github\.com/{re.escape(fields['repo'])}/pull/[1-9][0-9]*", fields["url"]
    ):
        raise ValueError("Source URL does not match repository")
    package = source.get("package_name", fields["repo"].split("/")[-1])
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", package):
        raise ValueError("Supply a single top-level Python package_name")
    return {**fields, "package_name": package}


def _dependency_recipe(recipe: str, source: dict) -> str:
    """A deliberately small trusted dependency grammar, not a general Docker sandbox."""
    lines = [s.strip() for s in recipe.replace("\\\n", " ").splitlines() if s.strip()]
    lines = [line for line in lines if not line.startswith("#")]
    if not lines or not re.fullmatch(
        r"FROM (?:docker\.io/library/)?python:[A-Za-z0-9_.-]+@sha256:[0-9a-f]{64}", lines[0]
    ):
        raise ValueError("Dependencies require a clean digest-pinned official Python base")
    # No arbitrary RUN, scripts, COPY, ADD, URLs, context mounts or opaque custom images.
    targets = {
        source["package_name"].lower().replace("_", "-"),
        source["repo"].split("/")[-1].lower(),
    }
    for line in lines[1:]:
        if line == APT_STANZA:
            continue
        prefix = "RUN python -m pip install "
        if not line.startswith(prefix):
            raise ValueError("Dependency recipe permits only pinned Python dependency installs")
        tokens = shlex.split(line[len(prefix) :])
        if not tokens:
            raise ValueError("Empty dependency install")
        if "--index-url" in tokens:
            index = tokens.index("--index-url")
            if tokens[index : index + 2] != ["--index-url", "https://download.pytorch.org/whl/cpu"]:
                raise ValueError("Only the explicit PyTorch CPU index is supported")
            del tokens[index : index + 2]
        for token in tokens:
            if token == "--no-cache-dir":
                continue
            match = re.fullmatch(
                r"([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([A-Za-z0-9_.+!-]+)", token
            )
            if not match:
                raise ValueError(f"Unsupported dependency token: {token}")
            if re.sub(r"[-_.]+", "-", match[1]).lower() in targets:
                raise ValueError("Dependency image must not install the target published package")
    clean = "\n".join(lines) + "\n"
    validate_dependency_pins(clean)
    return clean


def prepared_recipe(dependency_recipe: str, source: dict) -> str:
    """Build from base source remotely; source-only editable install is strictly offline.

    The caller supplies a controller-reviewed dependency recipe. Its explicit pins
    must include all build/runtime requirements. No compiled extension is supported.
    """
    source = _source(source)
    recipe = _dependency_recipe(dependency_recipe, source)
    package = source["package_name"]
    fetch = (
        "import io,tarfile,urllib.request,pathlib; "
        f"data=urllib.request.urlopen('https://codeload.github.com/{source['repo']}/tar.gz/{source['base_sha']}',timeout=60).read(); "
        "archive=tarfile.open(fileobj=io.BytesIO(data)); "
        "members=archive.getmembers(); prefix=members[0].name.split('/')[0]+'/'; "
        "members=[m for m in members if m.name.startswith(prefix) and m.name!=prefix]; "
        "[(setattr(m,'name',m.name[len(prefix):])) for m in members]; "
        "archive.extractall('/workspace',members=members,filter='data')"
    )
    origin = (
        "import importlib.util,pathlib; "
        f"s=importlib.util.find_spec({package!r}); "
        "p=pathlib.Path(s.origin).resolve() if s and s.origin else None; "
        "assert p and p.is_relative_to('/workspace') and p.suffix=='.py', 'Target package not imported from base source'; "
        "assert not any(p.suffix in {'.so','.pyd','.dll','.dylib'} for p in pathlib.Path('/workspace').rglob('*')), 'Compiled source unsupported'"
    )
    return (
        recipe
        + "RUN python -I -c "
        + shlex.quote(
            "import importlib.util; assert importlib.util.find_spec("
            + repr(package)
            + ") is None, 'Target package already exists in dependencies'"
        )
        + "\n"
        + "WORKDIR /workspace\n"
        + "RUN python -c "
        + shlex.quote(fetch)
        + "\n"
        + "RUN python -m pip install --no-index --no-deps --no-build-isolation --editable /workspace\n"
        + "RUN python -I -c "
        + shlex.quote(origin)
        + "\n"
    )


def collection_inventory(root: Path, source_paths: list[str]) -> dict[str, dict]:
    """Hash every declared submission file, including newly created helpers.

    Invoke in the trusted transfer/controller boundary before and after transport.
    Do not accept an inventory produced by the solver as a collection receipt.
    """
    root = root.resolve()
    result = {}
    for name in source_paths:
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or str(p) != name or not p.parts:
            raise ValueError("Unsafe collection path")
        target = root / name
        if any(part.is_symlink() for part in (target, *target.parents) if part != root.parent):
            raise ValueError("Linked collection path")
        if not target.exists():
            raise ValueError(f"Missing declared submission: {name}")
        for file in sorted([target] if target.is_file() else target.rglob("*")):
            if file.is_symlink() or not (file.is_file() or file.is_dir()):
                raise ValueError(f"Non-regular collection entry: {file}")
            if file.is_dir():
                continue
            relative = file.relative_to(root).as_posix()
            if (
                ".git" in file.relative_to(root).parts
                or file.suffix == ".pyc"
                or "__pycache__" in file.parts
            ):
                continue  # Exactly the declared Harbor transport exclusions.
            if file.stat().st_nlink != 1:
                raise ValueError(f"Hard-linked collection entry: {file}")
            data = file.read_bytes()
            result[relative] = {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
    if not result:
        raise ValueError("Empty submission collection")
    return result


def verify_collection(expected: dict, actual: dict) -> str:
    """Compare trusted pre/post-transfer inventories, never just root existence."""
    if not expected or expected != actual:
        raise ValueError("Submission collection incomplete or changed during transfer")
    for name, item in expected.items():
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(item, dict)
            or set(item) != {"sha256", "size_bytes"}
            or not isinstance(item["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
            or type(item["size_bytes"]) is not int
            or item["size_bytes"] < 0
            or PurePosixPath(name).is_absolute()
            or ".." in PurePosixPath(name).parts
            or str(PurePosixPath(name)) != name
            or not PurePosixPath(name).parts
        ):
            raise ValueError("Invalid collection inventory")
    return hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()


# This trusted script is serialized, never imported or executed by the emitter.
RUNNER = r"""from __future__ import annotations
import hashlib, json, pathlib, subprocess, sys, xml.etree.ElementTree as ET
out = pathlib.Path('/logs/verifier')
out.mkdir(parents=True, exist_ok=True)
reward = out / 'reward.txt'
reward.unlink(missing_ok=True)
(out / 'reward.json').unlink(missing_ok=True)
details = {'valid': False, 'outcome': 'incomplete', 'reason': 'not executed'}
try:
    contract = json.loads(pathlib.Path('/tests/contract.json').read_text())
    materialization = json.loads(pathlib.Path('/tests/materialization.json').read_text())
    inventory = {}
    for name in contract['source_paths']:
        root = pathlib.Path('/workspace') / name
        if not root.exists() or root.is_symlink():
            raise ValueError('Missing or linked submission: ' + name)
        for p in ([root] if root.is_file() else sorted(root.rglob('*'))):
            if p.is_symlink() or not (p.is_file() or p.is_dir()):
                raise ValueError('Non-regular submission: ' + str(p))
            if p.is_file():
                if p.stat().st_nlink != 1:
                    raise ValueError('Hard-linked submission: ' + str(p))
                if p.suffix in {'.so', '.pyd', '.dll', '.dylib'}:
                    raise ValueError('Compiled submission unsupported')
                if p.suffix == '.pyc' or '__pycache__' in p.parts or '.git' in p.parts:
                    raise ValueError('Unexpected excluded submission artifact')
                data = p.read_bytes()
                inventory[p.relative_to('/workspace').as_posix()] = {'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data)}
    if not inventory:
        raise ValueError('Empty submission')
    (out / 'collection.json').write_text(json.dumps(inventory, sort_keys=True))
    # This is an origin check without importing submitted code into the protected process.
    origin_code = "import importlib.util,json; s=importlib.util.find_spec(" + repr(materialization['package_name']) + "); print(json.dumps(s.origin if s else None))"
    checked = subprocess.run(['/usr/sbin/runuser','-u','agent','--','/usr/local/bin/python','-I','-c',origin_code], cwd='/workspace', capture_output=True, text=True, timeout=15)
    if checked.returncode:
        raise ValueError('Source origin check did not execute')
    origin = pathlib.Path(json.loads(checked.stdout)).resolve()
    if not origin.is_relative_to('/workspace') or origin.suffix != '.py' or not origin.is_file():
        raise ValueError('Executed package is not transferred Python source')
    if not any(origin.is_relative_to(pathlib.Path('/workspace') / p) or origin == pathlib.Path('/workspace') / p for p in contract['source_paths']):
        raise ValueError('Package origin is outside declared collection')
    witness = pathlib.Path('/tests/source_origin_probe.py')
    if witness.is_file():
        observation = subprocess.run(['/usr/sbin/runuser','-u','agent','--','/usr/local/bin/python','-I','-c',witness.read_text()], cwd='/workspace', capture_output=True, text=True, timeout=60)
        if observation.returncode or len(observation.stdout) > 1000000:
            raise ValueError('Source observation did not execute')
        value = json.loads(observation.stdout)
        (out / 'source-observation.json').write_text(json.dumps({'value':value,'package_origin':str(origin),'origin_sha256':hashlib.sha256(origin.read_bytes()).hexdigest()}, allow_nan=False))
    report = pathlib.Path('/tmp/tasksmith-pytest/junit.xml')
    report.parent.mkdir(mode=0o700, exist_ok=True)
    report.unlink(missing_ok=True)
    env = {'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':'/home/agent',
           'PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1','PYTHONHASHSEED':'0','HF_HUB_OFFLINE':'1',
           'TRANSFORMERS_OFFLINE':'1','TOKENIZERS_PARALLELISM':'false','OMP_NUM_THREADS':'1'}
    result = subprocess.run([sys.executable,'-I','-m','pytest','-p','no:cacheprovider','--confcutdir=/tests','-c','/tests/pytest.ini','/tests/test_contract.py','-v','--junitxml='+str(report)], cwd='/tests', env=env, capture_output=True, text=True, timeout=240)
    (out / 'pytest-output.txt').write_text(result.stdout+'\n'+result.stderr)
    if result.returncode not in (0, 1) or not report.is_file():
        raise ValueError('Protected test execution/collection incomplete')
    cases = ET.parse(report).findall('.//testcase')
    required = {t for r in contract['requirements'] for t in r['tests']}
    observed = {c.attrib.get('name','').split('[')[0] for c in cases}
    if len(cases) < contract['min_tests'] or not required <= observed or any(c.find('skipped') is not None or c.find('error') is not None for c in cases):
        raise ValueError('Missing, skipped, or errored protected tests')
    failed = [c.attrib.get('name') for c in cases if c.find('failure') is not None]
    passed = result.returncode == 0 and not failed
    details = {'valid': True, 'outcome': 'passed' if passed else 'submission_failure',
               'reward': int(passed), 'n_tests':len(cases), 'failed':failed,
               'origin':str(origin), 'collection_digest':hashlib.sha256(json.dumps(inventory,sort_keys=True).encode()).hexdigest(),
               'controller_collection_comparison_required':True}
    reward.write_text(str(int(passed))+'\n')
except Exception as exc:
    details = {'valid':False, 'outcome':'incomplete', 'reason':type(exc).__name__+': '+str(exc)}
(out / 'details.json').write_text(json.dumps(details,indent=2))
if not details['valid']:
    sys.exit(2)
"""


def emit_task(
    path: Path,
    source: dict,
    dependency_recipe: str,
    *,
    execution_contract: Contract,
    instruction: str,
    solution_script: str,
    protected_tests: str,
    source_origin_probe: str | None = None,
) -> dict:
    """Emit an immutable fresh task; receipt is provenance, never author acceptance.

    Harbor reward remains provisional until the trial controller verifies trusted
    pre/post-transfer inventories and the profile's source-mutation witness.
    """
    source = _source(source)
    if path.exists() or path.is_symlink():
        raise FileExistsError("Task destination already exists")
    recipe = prepared_recipe(dependency_recipe, source)
    if not instruction.strip() or not solution_script.strip():
        raise ValueError("Instruction and solution must be nonempty")
    # Source-only supports Python package directories and declared Python helpers,
    # not changes to build/install metadata that an editable seed could ignore.
    for name in execution_contract.source_paths:
        p = PurePosixPath(name)
        if p.suffix and p.suffix != ".py":
            raise ValueError("Source-only collection cannot include build/config/binary files")
    package_roots = [
        PurePosixPath(source["package_name"]),
        PurePosixPath("src") / source["package_name"],
    ]
    if not any(
        PurePosixPath(name) == root or PurePosixPath(name) in root.parents
        for name in execution_contract.source_paths
        for root in package_roots
    ):
        raise ValueError("Collect the complete package directory, including possible new helpers")
    materialization = {
        "policy_version": POLICY_VERSION,
        "mode": "source_only",
        "package_name": source["package_name"],
        "source_paths": execution_contract.source_paths,
        "offline": True,
        "requires_controller_collection_comparison": True,
        "requires_source_change_witness": True,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".tasksmith-emit-", dir=path.parent) as temporary:
        staging = Path(temporary) / "task"
        for directory in ("environment", "solution", "tests"):
            (staging / directory).mkdir(parents=True)
        for name, content in {
            "instruction.md": instruction,
            "environment/Dockerfile": recipe,
            "solution/solve.sh": solution_script,
            "tests/test_contract.py": protected_tests,
            "contract.json": execution_contract.model_dump_json(indent=2),
        }.items():
            (staging / name).write_text(content)
        finalize(staging, source)
        (staging / "tests/runner.py").write_text(RUNNER)
        if source_origin_probe:
            (staging / "tests/source_origin_probe.py").write_text(source_origin_probe)
        (staging / "tests/materialization.json").write_text(json.dumps(materialization, indent=2))
        task = tomllib.loads((staging / "task.toml").read_text())
        task["metadata"]["repo2env"]["pipeline"] = "tasksmith"
        from harbor.models.task.config import TaskConfig

        emitted = tomli_w.dumps(task)
        parsed = TaskConfig.model_validate(tomllib.loads(emitted)).model_dump(mode="json")
        if (
            parsed["schema_version"] != "1.4"
            or parsed["verifier"]["environment_mode"] != "separate"
            or parsed["environment"]["network_mode"] != "no-network"
            or parsed["verifier"]["environment"]["network_mode"] != "no-network"
            or [a["source"] for a in parsed["artifacts"]]
            != ["/workspace/" + p for p in execution_contract.source_paths]
        ):
            raise ValueError("Harbor round-trip lost required policy")
        (staging / "task.toml").write_text(emitted)
        receipt = {
            "policy_version": POLICY_VERSION,
            "source": source,
            "task_digest": digest_task(staging),
            "dependency_recipe_digest": hashlib.sha256(
                _dependency_recipe(dependency_recipe, source).encode()
            ).hexdigest(),
            "materialization": materialization,
            "accepted": False,
        }
        staging.rename(path)
    return receipt
