from __future__ import annotations

import ast
import hashlib
import json
import re
import shlex
import shutil
import tempfile
from functools import lru_cache
from pathlib import Path

import tomli_w

from repo2rlenv.curation.models import Contract


def validate_dependency_pins(recipe: str) -> None:
    """Catch direct floating dependencies before paying to build or run a task."""
    for line in recipe.replace("\\\n", " ").splitlines():
        match = re.search(r"\bpip\s+install\s+(.+)", line)
        if not match:
            continue
        tokens = shlex.split(match.group(1).split("&&")[0])
        skip = False
        for token in tokens:
            if skip:
                skip = False
                continue
            if token in {"--index-url", "--extra-index-url", "-e", "--editable", "--find-links"}:
                skip = True
            elif token in {"-r", "--requirement", "-c", "--constraint"}:
                raise ValueError(
                    "Use explicit pinned dependencies in the self-contained Dockerfile"
                )
            elif token.startswith("-"):
                continue
            elif not re.fullmatch(r"[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[^*\s]+", token):
                raise ValueError(f"Unpinned build dependency: {token}")


@lru_cache(maxsize=32)
def pin_docker_base(image: str) -> str:
    """Resolve Docker Hub tag metadata; no image layers are downloaded locally."""
    import urllib.parse
    import urllib.request

    if "@sha256:" in image:
        return image
    if "/" in image.split(":")[0]:
        raise ValueError("Supply an explicit @sha256 digest for non-library base images")
    name, _, tag = image.partition(":")
    repo = "library/" + name
    url = "https://auth.docker.io/token?" + urllib.parse.urlencode(
        {"service": "registry.docker.io", "scope": f"repository:{repo}:pull"}
    )
    with urllib.request.urlopen(url, timeout=30) as response:
        token = json.load(response)["token"]
    request = urllib.request.Request(
        f"https://registry-1.docker.io/v2/{repo}/manifests/{tag or 'latest'}",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json",
        },
        method="HEAD",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        digest = response.headers["Docker-Content-Digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest or ""):
        raise ValueError("Registry did not return a content digest")
    return image + "@" + digest


HARDEN = r"""
# repo2rlenv isolation: host-side agent, immutable dependencies, no history.
USER root
RUN id agent >/dev/null 2>&1 || useradd -m -u 1000 -s /bin/bash agent
RUN find /workspace /root /tmp -name .git -prune -exec rm -rf {} + \
 && rm -rf /root/.cache /root/.ssh /root/.gitconfig \
 && find / -xdev -type f -perm /6000 -exec chmod a-s {} + 2>/dev/null \
 && mkdir -p /logs/verifier /logs/agent /logs/artifacts \
 && chown -R agent:agent /workspace /logs/agent /logs/artifacts \
 && chmod 755 /logs/verifier
ENV PYTHONHASHSEED=0 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false
WORKDIR /workspace
"""

VERIFIER = r"""from __future__ import annotations
import json, pathlib, subprocess, sys, xml.etree.ElementTree as ET

out = pathlib.Path('/logs/verifier')
out.mkdir(parents=True, exist_ok=True)
reward = out / 'reward.txt'
reward.write_text('0\n')
contract = json.loads(pathlib.Path('/tests/contract.json').read_text())
details = {'valid': False, 'reason': 'not executed'}
try:
    for name in contract['source_paths']:
        root = pathlib.Path('/workspace') / name
        if not root.exists() or root.is_symlink():
            raise ValueError('Missing or linked submission: ' + name)
        for p in ([root] if root.is_file() else root.rglob('*')):
            if p.is_symlink() or (not p.is_file() and not p.is_dir()):
                raise ValueError('Non-regular submission entry: ' + str(p))
    report_dir = pathlib.Path('/tmp/r2e-pytest')
    report_dir.mkdir(mode=0o700, exist_ok=True)
    report = report_dir / 'junit.xml'
    report.unlink(missing_ok=True)
    env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/home/agent',
           'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'PYTHONHASHSEED': '0',
           'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
           'TOKENIZERS_PARALLELISM': 'false', 'OMP_NUM_THREADS': '1'}
    result = subprocess.run(
        [sys.executable, '-I', '-m',
         'pytest', '-p', 'no:cacheprovider', '--confcutdir=/tests', '-c', '/tests/pytest.ini',
         '/tests/test_contract.py', '-v', '--junitxml=' + str(report)],
        cwd='/tests', env=env, capture_output=True, text=True, timeout=240)
    (out / 'pytest-output.txt').write_text(result.stdout + '\n' + result.stderr)
    if not report.exists():
        raise ValueError('No test report')
    tree = ET.parse(report)
    cases = tree.findall('.//testcase')
    failed = [c.attrib.get('name') for c in cases if
              c.find('failure') is not None or c.find('error') is not None or c.find('skipped') is not None]
    observed = {c.attrib.get('name', '').split('[')[0] for c in cases}
    required = {name for r in contract['requirements'] for name in r['tests']}
    valid = (result.returncode == 0 and len(cases) >= contract['min_tests']
             and required <= observed and not failed)
    details = {'valid': valid, 'exit_code': result.returncode, 'n_tests': len(cases),
               'failed_or_skipped': failed, 'missing_required_tests': sorted(required - observed),
               'tests': [c.attrib.get('name') for c in cases]}
    if valid:
        reward.write_text('1\n')
except Exception as exc:
    details = {'valid': False, 'reason': type(exc).__name__ + ': ' + str(exc)}
(out / 'details.json').write_text(json.dumps(details, indent=2))
"""


def validate_probe_tests(text: str) -> None:
    """Keep protected assertions in a clean interpreter, away from editable code."""
    allowed = {
        "collections",
        "contextlib",
        "functools",
        "itertools",
        "json",
        "math",
        "random",
        "re",
        "statistics",
        "textwrap",
        "typing",
        "pytest",
        "numpy",
        "probe",
    }
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise ValueError(f"Invalid test syntax: {exc}") from exc
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            if any(name.split(".")[0] not in allowed for name in names):
                raise ValueError(
                    "Protected tests may import only standard math/test helpers, numpy and probe. "
                    "Import submitted packages inside run_probe code strings."
                )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"eval", "exec", "__import__", "compile", "open"}
        ):
            raise ValueError("Protected tests may not execute or load submitted files directly")
    if not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_probe"
        for node in ast.walk(tree)
    ):
        raise ValueError("Tests must observe submitted behavior through run_probe")


def digest_task(path: Path) -> str:
    digest = hashlib.sha256()
    for p in sorted(path.rglob("*")):
        if p.is_symlink():
            raise ValueError(f"Symlink in task: {p}")
        if p.is_file():
            digest.update(p.relative_to(path).as_posix().encode() + b"\0" + p.read_bytes() + b"\0")
    return digest.hexdigest()


def finalize(path: Path, source: dict) -> Contract:
    """Own the isolation/reward wrapper; author owns behavior and its tests."""
    required = [
        "instruction.md",
        "environment/Dockerfile",
        "solution/solve.sh",
        "tests/test_contract.py",
        "contract.json",
    ]
    for name in required:
        p = path / name
        if not p.is_file() or p.is_symlink() or not p.read_text().strip():
            raise ValueError(f"Missing regular task file: {name}")
    digest_task(path)
    contract = Contract.model_validate_json((path / "contract.json").read_text())
    validate_probe_tests((path / "tests/test_contract.py").read_text())
    if contract.reward_mode != "deterministic":
        raise ValueError(
            "Judge rewards are review-only: no deterministic release without a verifier"
        )
    instruction = (path / "instruction.md").read_text()
    if re.search(r"github\.com/[^\s]+/pull/|\b[0-9a-f]{40}\b", instruction):
        raise ValueError("Instruction exposes PR or commit provenance")
    for requirement in contract.requirements:
        for test in requirement.tests:
            if not re.search(
                r"def\s+" + re.escape(test) + r"\s*\(",
                (path / "tests/test_contract.py").read_text(),
            ):
                raise ValueError(f"Coverage map references absent test: {test}")
    for mutation in [*contract.mutations, *contract.equivalents]:
        if not re.fullmatch(r"[a-z0-9_-]{1,50}", mutation.name):
            raise ValueError("Mutation name must be a safe identifier")
    recipe = (
        (path / "environment/Dockerfile").read_text().split("# repo2rlenv isolation:")[0].rstrip()
    )
    for forbidden in [
        source["head_sha"],
        "/private/",
        "gold.patch",
        "/solution",
        "/tests",
        "git clone",
    ]:
        if forbidden in recipe:
            raise ValueError(f"Forbidden build input: {forbidden}")
    if source["base_sha"] not in recipe:
        raise ValueError("Build recipe must fetch the immutable base revision")
    if "WORKDIR /workspace" not in recipe:
        raise ValueError("Build recipe must use /workspace")
    validate_dependency_pins(recipe)
    bases = re.findall(r"^FROM\s+(\S+)", recipe, re.MULTILINE)
    if len(bases) != 1:
        raise ValueError("This profile requires one explicit base image")
    recipe = recipe.replace("FROM " + bases[0], "FROM " + pin_docker_base(bases[0]), 1)
    if (path / "environment/docker-compose.yaml").exists():
        raise ValueError("Compose is not supported by this isolated curation profile")
    # All build context must be explicit. Avoid author-provided hidden sidecars.
    for p in (path / "environment").rglob("*"):
        if p.is_file() and p.name != "Dockerfile":
            raise ValueError(
                "Use a self-contained Dockerfile; external assets must be pinned in its build"
            )
    recipe += "\n" + HARDEN
    (path / "environment/Dockerfile").write_text(recipe)
    tests = path / "tests"
    (tests / "contract.json").write_text(contract.model_dump_json(indent=2))
    (tests / "runner.py").write_text(VERIFIER)
    (tests / "probe.py").write_text(Path(__file__).with_name("probe_runtime.py").read_text())
    (tests / "pytest.ini").write_text("[pytest]\naddopts =\n")
    (tests / "test.sh").write_text(
        "#!/bin/bash\nset -eu\nchmod -R go-rwx /tests /opt/r2e-grader\n"
        "exec /opt/r2e-grader/bin/python -I /tests/runner.py\n"
    )
    # The clean grading image discards its base copy of submitted source paths,
    # then Harbor imports only those paths from the finished solver sandbox.
    paths = " ".join(shlex.quote("/workspace/" + p) for p in contract.source_paths)
    (tests / "Dockerfile").write_text(
        recipe
        + "\nRUN python -m venv /opt/r2e-grader"
        + " && /opt/r2e-grader/bin/pip install --no-cache-dir pytest==8.4.2 numpy==2.2.6\n"
        + f"RUN rm -rf {paths}\nCOPY . /tests/\nRUN chmod -R go-rwx /tests /opt/r2e-grader\n"
    )
    task = {
        "schema_version": "1.4",
        "task": {
            "name": "repo2rlenv/" + source["id"],
            "version": "1.0.0",
            "description": contract.title,
        },
        "metadata": {
            "repo2env": {
                "pipeline": "dynamic_curation",
                "pr_url": source["url"],
                "base_commit": source["base_sha"],
                "reward_kinds": ["test_execution"],
            }
        },
        "agent": {"timeout_sec": 900, "user": "agent"},
        "environment": {
            "network_mode": "no-network",
            "cpus": 2,
            "memory_mb": 8192,
            "build_timeout_sec": 900,
        },
        "verifier": {
            "timeout_sec": 300,
            "user": "root",
            "environment_mode": "separate",
            "environment": {
                "network_mode": "no-network",
                "cpus": 2,
                "memory_mb": 8192,
                "build_timeout_sec": 900,
            },
        },
        "artifacts": [
            {"source": "/workspace/" + p, "exclude": ["__pycache__", "*.pyc", ".git"]}
            for p in contract.source_paths
        ],
    }
    from harbor.models.task.config import TaskConfig

    TaskConfig.model_validate(task)
    (path / "task.toml").write_text(tomli_w.dumps(task))
    return contract


def release_task(source: Path, destination: Path) -> None:
    expected = digest_task(source)
    if destination.exists():
        if digest_task(destination) == expected:
            return
        raise ValueError(f"Refusing to overwrite released task: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".release-", dir=destination.parent) as tmp:
        staged = Path(tmp) / "task"
        shutil.copytree(source, staged)
        if digest_task(staged) != expected:
            raise ValueError("Task changed during release")
        staged.rename(destination)
