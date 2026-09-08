from __future__ import annotations

import ast
import hashlib
import json
import shlex
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from repo2rlenv.tasksmith import build
from repo2rlenv.tasksmith.authoring import Discovery
from repo2rlenv.tasksmith.config import TasksmithConfig
from repo2rlenv.tasksmith.emit import APT_STANZA
from repo2rlenv.tasksmith.worker import save_json


@pytest.fixture
def case(tmp_path, monkeypatch):
    module = "tests/test_batches.py"
    original = Discovery(
        useful_outcome="Preserve the complete public batching behavior.",
        behaviors=[
            {
                "id": "batching",
                "outcome": "Return the correct logical batch sequence.",
                "source_evidence": ["library/batches.py"],
            }
        ],
        dependency_dockerfile=f"FROM python:3.12-slim@sha256:{'c' * 64}\n{APT_STANZA}\nRUN python -m pip install --no-cache-dir pytest==8.3.5\n",
        dependency_inputs={"tests": "pytest==8.3.5"},
        readiness_commands=["python -c 'import library'"],
        upstream_test_commands=[f"python -m pytest {module}::test_invented -q"],
        upstream_tests="relevant_tests",
        resource_rationale="Small explicit CPU fixtures exercise the public behavior.",
    )
    c = SimpleNamespace(
        source={
            "id": "example-library-12",
            "repo": "example/library",
            "url": "https://github.com/example/library/pull/12",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
        },
        config=TasksmithConfig(
            ledger_path=tmp_path / "budget.json", ledger_limit_usd=50, campaign_id="same-campaign"
        ),
        original=original,
        corrected=original.model_copy(
            update={"upstream_test_commands": [f"python -m pytest {module}::test_dynamic -q"]}
        ),
        module=module,
        root=tmp_path / "construct",
        cache=tmp_path / "cache",
        before="def test_existing():\n    assert True\n",
        head="def test_existing():\n    assert True\n\ndef test_dynamic():\n    assert True\n",
        events=[],
        stages=[],
        workspaces=[],
        zero_execution=False,
        empty_selection=False,
        remote_files={},
        reads=[],
        parameter_count=1,
    )
    monkeypatch.setattr(build.time, "time", lambda: 1000)

    async def shell(command, timeout_sec=120):
        c.events.append(command)
        result = {"exit_code": 0, "stdout": "success", "stderr": ""}
        if "tasksmith-selector-source-capture" in command:
            ast.parse(shlex.split(command)[-2])
            args = json.loads(shlex.split(command)[-1])
            text = c.before if args["commit"] == c.source["base_sha"] else c.head
            data = b"" if text is None else text.encode()
            receipt = {
                "commit": args["commit"],
                "module": args["module"],
                "status": "missing" if text is None else "present",
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            if len(data) > args["limit"]:
                receipt.update(status="error", reason="Pinned test source exceeds transfer bound")
            c.remote_files[args["receipt_path"]] = json.dumps(receipt).encode()
            c.remote_files[args["data_path"]] = data
            # Model the provider's actual stdout tail: source transfer must ignore it.
            result["stdout"] = data[-20_000:].decode()
        elif "tasksmith-pytest-observation-capture" in command:
            # Parse the trusted remote observer as data; never run pytest or target code here.
            ast.parse(shlex.split(command)[-2])
            capture = json.loads(shlex.split(command)[-1])
            args = capture["args"]
            targeted = "::" in args[0]
            names = ["test_existing", "test_dynamic"] if not targeted else [args[0].split("::")[1]]
            parameters = ["small"] if c.parameter_count == 1 else map(str, range(c.parameter_count))
            nodes = [f"{module}::{name}[{parameter}]" for parameter in parameters for name in names]
            if targeted and c.empty_selection:
                nodes = []
            report = {
                "exit_code": 0,
                "collected": nodes,
                "executed": [] if "--collect-only" in args or c.zero_execution else nodes,
                "stdout": "",
                "stderr": "",
            }
            data = json.dumps(report).encode()
            c.remote_files[capture["receipt_path"]] = json.dumps(
                {
                    "input_digest": capture["input_digest"],
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "status": "present" if len(data) <= capture["limit"] else "oversized",
                }
            ).encode()
            c.remote_files[capture["data_path"]] = data
            result["stdout"] = data[-20_000:].decode()
        elif "test_invented" in command:
            result.update(
                exit_code=4,
                stdout="no tests ran",
                stderr="ERROR: not found: " + module + "::test_invented",
            )
        return json.dumps(result)

    c.shell = shell

    async def read(path):
        c.reads.append(path)
        return c.remote_files[path]

    c.read = read

    @asynccontextmanager
    async def workspace(wc, root, budget, allowance):
        c.workspaces.append((wc, budget))
        receipt = {"status": "running", "image_id": "image-existing", "resource_id": "sandbox"}
        save_json(root / "resource.json", receipt)
        try:
            yield SimpleNamespace(shell=shell, read=read)
        finally:
            save_json(root / "resource.json", {**receipt, "status": "terminated"})

    async def prepare(*args):
        return {}

    async def artifact(**kwargs):
        c.stages.append(kwargs)
        assert kwargs["schema"] is Discovery
        assert kwargs["stage"] == "bootstrap-selector-repair-1"
        assert kwargs["shell"] is None  # no model-driven environment mutation
        await kwargs["validate"](c.corrected)
        save_json(kwargs["root"] / "artifact.json", {"artifact": c.corrected.model_dump()})
        return c.corrected

    monkeypatch.setattr(build, "budgeted_workspace", workspace)
    monkeypatch.setattr(build, "prepare_checked", prepare)
    monkeypatch.setattr(build, "artifact_stage", artifact)
    return c


@asynccontextmanager
async def ready(c):
    async with build.bootstrap_ready(
        c.config, c.source, c.original, c.root, c.cache, 2000, c.config.budget(c.source["id"])
    ) as value:
        yield value


@pytest.mark.asyncio
async def test_invented_selector_corrected_in_same_workspace_with_real_source_and_execution(case):
    c = case
    async with ready(c) as (discovered, wc, readiness, _):
        assert readiness["passed"] and discovered == c.corrected
        assert wc.deadline == 2000
        assert readiness["checks"][-1]["executed"] == [f"{c.module}::test_dynamic[small]"]
        assert readiness["selector_proof"]["required_changed_anchors"] == [
            f"{c.module}::test_dynamic"
        ]
    assert len(c.workspaces) == len(c.stages) == 1
    assert c.workspaces[0][1] is c.stages[0]["budget"]
    assert c.corrected.dependency_dockerfile == c.original.dependency_dockerfile
    assert not json.loads((c.root / "readiness.json").read_text())["passed"]
    assert json.loads((c.root / "selector-readiness-1/readiness.json").read_text())["passed"]
    evidence = c.stages[0]["inputs"]["upstream_source_and_collection"]
    assert evidence["source_files"][c.module] == {"before": c.before, "head": c.head}
    assert "bootstrap-attempt-2" not in str(c.workspaces)
    # Full-module collection, corrected collection, and actual execution; cached validation
    # does not repeat the corrected collection or spend another worker invocation.
    assert sum("tasksmith-pytest-observation-capture" in event for event in c.events) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,message",
    [
        ("unrelated", "same upstream module files"),
        ("legacy_only", "omit changed collectable"),
        ("recipe", "cannot weaken or replace dependency_dockerfile"),
        ("outcome", "cannot weaken or replace useful_outcome"),
        ("empty", "collect nonzero tests"),
    ],
)
async def test_selector_correction_cannot_weaken_scope_or_collection(case, change, message):
    c = case
    if change == "unrelated":
        c.corrected = c.corrected.model_copy(
            update={"upstream_test_commands": ["pytest tests/test_unrelated.py -q"]}
        )
    elif change == "legacy_only":
        c.corrected = c.corrected.model_copy(
            update={"upstream_test_commands": [f"pytest {c.module}::test_existing -q"]}
        )
    elif change == "recipe":
        c.corrected = c.corrected.model_copy(
            update={
                "dependency_dockerfile": c.corrected.dependency_dockerfile.replace("8.3.5", "8.3.4")
            }
        )
    elif change == "outcome":
        c.corrected = c.corrected.model_copy(
            update={"useful_outcome": "Only the old trivial cases now matter."}
        )
    else:
        c.empty_selection = True
    with pytest.raises(ValueError, match=message):
        async with ready(c):
            pytest.fail("Weak selector correction advanced")
    assert len(c.workspaces) == 1 and not (c.root / "construction.json").exists()
    assert not (c.root / "selector-readiness-1/readiness.json").exists()
    if change in {"empty", "legacy_only"}:
        checks = list((c.root / "bootstrap-selector-repair-1/selection-checks").glob("*.json"))
        assert len(checks) == 1 and not json.loads(checks[0].read_text())["passed"]


@pytest.mark.asyncio
async def test_exit_zero_without_executed_tests_is_not_readiness(case):
    c = case
    c.zero_execution = True
    with pytest.raises(build.BootstrapReadinessError, match="did not execute required tests"):
        async with ready(c):
            pytest.fail("Skipped-only corrected tests advanced")
    report = json.loads((c.root / "selector-readiness-1/readiness.json").read_text())
    assert not report["passed"] and report["execution_errors"]
    assert len(c.workspaces) == 1


@pytest.mark.asyncio
async def test_retained_selector_failure_does_not_rebuild_or_reset_repair(case):
    c = case
    c.empty_selection = True
    with pytest.raises(ValueError, match="collect nonzero"):
        async with ready(c):
            pytest.fail("Empty selection advanced")
    original = (c.root / "readiness.json").read_bytes()
    with pytest.raises(build.BootstrapReadinessError, match="explicit workspace reconciliation"):
        async with ready(c):
            pytest.fail("Blind selector restart advanced")
    assert len(c.workspaces) == len(c.stages) == 1
    assert (c.root / "readiness.json").read_bytes() == original


@pytest.mark.parametrize(
    "code,output,expected",
    [
        (4, "ERROR: not found: tests/test_batches.py::missing", True),
        (5, "no tests collected", True),
        (5, "collected 0 items", True),
        (1, "AssertionError: expected no tests collected", False),
        (2, "ImportError: package unavailable", False),
        (0, "no tests collected", False),
        (4, "ERROR: found no collectors for tests/test_batches.py::test_dynamic", False),
        (
            5,
            "collected 0 items\nERROR collecting tests/test_batches.py\nRuntimeError: broken",
            False,
        ),
        (4, "SyntaxError: invalid syntax\nERROR: not found: tests/test_batches.py", False),
    ],
)
def test_selector_repair_trigger_is_explicit_selection_failure(case, code, output, expected):
    readiness = {
        "passed": False,
        "checks": [
            {
                "command": case.original.upstream_test_commands[0],
                "exit_code": code,
                "stderr": output,
            }
        ],
    }
    assert build._selector_failure(case.original, readiness) is expected


@pytest.mark.parametrize(
    "command",
    [
        "pytest ../tests/test_x.py",
        "pytest /tests/test_x.py",
        "pytest tests/test_x.py; true",
        "pytest tests/test_x.py -p injected",
        "python -m pytest tests -q",
    ],
)
def test_selector_commands_have_bounded_explicit_modules(command):
    with pytest.raises(ValueError):
        build._pytest_command(command)


@pytest.mark.asyncio
async def test_remote_observer_missing_report_fails_closed(case):
    async def shell(*args):
        return json.dumps({"exit_code": 0, "stdout": "truncated output", "stderr": ""})

    async def read(path):
        raise FileNotFoundError(path)

    with pytest.raises(build.BootstrapReadinessError, match="report missing"):
        await build._pytest_observation(
            shell, case.source, f"pytest {case.module}", collect_only=False, read=read
        )


@pytest.mark.asyncio
async def test_selector_source_evidence_cache_is_bound_and_preserves_failed_collection(
    case, monkeypatch
):
    c = case

    async def empty_collection(*args, **kwargs):
        return {
            "exit_code": 5,
            "collected": [],
            "executed": [],
            "stdout": "no tests collected",
            "stderr": "",
        }

    monkeypatch.setattr(build, "_pytest_observation", empty_collection)
    with pytest.raises(build.BootstrapReadinessError, match="did not collect successfully"):
        await build._selector_sources(c.shell, c.source, c.original, c.root, read=c.read)
    saved = json.loads((c.root / "source-and-collection.json").read_text())
    assert saved["full_collection"]["exit_code"] == 5
    assert saved["source_files"][c.module]["head"] == c.head
    with pytest.raises(
        build.BootstrapReadinessError, match="Retained full-module collection failed"
    ):
        await build._selector_sources(None, c.source, c.original, c.root)
    with pytest.raises(build.BootstrapReadinessError, match="different source/modules"):
        await build._selector_sources(None, {**c.source, "head_sha": "d" * 40}, c.original, c.root)


def test_dependency_import_failure_with_pytest_no_collectors_is_not_selector_repair(case):
    readiness = {
        "passed": False,
        "checks": [
            {
                "command": case.original.upstream_test_commands[0],
                "exit_code": 4,
                "stdout": "ERROR collecting tests/test_initialization.py\n"
                "ImportError while importing test module 'tests/test_initialization.py'.\n"
                "    from scipy import stats\nE   ModuleNotFoundError: No module named 'scipy'\n",
                "stderr": "ERROR: found no collectors for tests/test_initialization.py::TestLoraInitialization::test_init\n",
            }
        ],
    }
    assert not build._selector_failure(case.original, readiness)


@pytest.mark.asyncio
async def test_large_module_is_transferred_completely_before_ast_analysis(case):
    c = case
    c.before = (
        "def test_existing():\n    values = [\n"
        + "        1,\n" * 3_000
        + "    ]\n    assert values\n"
    )
    c.head = c.before + "\ndef test_dynamic():\n    assert True\n"
    assert len(c.head.encode()) > 20_000
    with pytest.raises(SyntaxError):
        ast.parse(c.head[-20_000:])
    evidence = await build._selector_sources(c.shell, c.source, c.original, c.root, read=c.read)
    assert evidence["source_files"][c.module] == {"before": c.before, "head": c.head}
    assert evidence["required_changed_anchors"] == [f"{c.module}::test_dynamic"]
    for label in ("before", "head"):
        data = getattr(c, label).encode()
        receipt = evidence["source_transfers"][c.module][label]
        assert receipt["size"] == len(data)
        assert receipt["sha256"] == hashlib.sha256(data).hexdigest()
    assert len(c.reads) == 6


@pytest.mark.asyncio
async def test_explicitly_absent_base_module_is_a_new_file(case):
    c = case
    c.before = None
    evidence = await build._selector_sources(c.shell, c.source, c.original, c.root, read=c.read)
    assert evidence["source_files"][c.module]["before"] == ""
    assert evidence["source_transfers"][c.module]["before"]["status"] == "missing"
    assert evidence["required_changed_anchors"] == [
        f"{c.module}::test_dynamic",
        f"{c.module}::test_existing",
    ]


@pytest.mark.asyncio
async def test_absent_head_module_fails_before_collection(case):
    c = case
    c.head = None
    with pytest.raises(build.BootstrapReadinessError, match="module is unavailable"):
        await build._selector_sources(c.shell, c.source, c.original, c.root, read=c.read)
    assert not any("tasksmith-pytest-observation-capture" in event for event in c.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_commit", "truncated", "identity", "read_bound"])
async def test_source_transfer_failures_cannot_become_empty_base_or_partial_ast(case, failure):
    c = case

    async def read(path):
        data = await c.read(path)
        if path.endswith(".json"):
            receipt = json.loads(data)
            if failure == "missing_commit":
                receipt.update(status="error", reason="Pinned commit unavailable")
            elif failure == "identity":
                receipt["commit"] = "d" * 40
            return json.dumps(receipt).encode()
        if failure == "read_bound":
            raise ValueError("Remote file exceeds configured read bound")
        return data[1:] if failure == "truncated" else data

    with pytest.raises(
        (build.BootstrapReadinessError, ValueError), match=r"unavailable|differs|bound"
    ):
        await build._selector_sources(c.shell, c.source, c.original, c.root, read=read)
    assert not any("tasksmith-pytest-observation-capture" in event for event in c.events)


@pytest.mark.asyncio
async def test_aggregate_source_bound_is_checked_before_reading_second_blob(case):
    c = case
    c.before = "#" + "x" * 65_000 + "\n"
    c.head = c.before + "def test_dynamic():\n    assert True\n"
    with pytest.raises(build.BootstrapReadinessError, match="transfer bound"):
        await build._selector_sources(c.shell, c.source, c.original, c.root, read=c.read)
    assert len([path for path in c.reads if path.endswith(".source")]) == 1
    assert not any("tasksmith-pytest-observation-capture" in event for event in c.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("collect_only", [True, False])
async def test_large_collection_and_execution_reports_do_not_use_stdout(case, collect_only):
    c = case
    c.parameter_count = 700
    report = await build._pytest_observation(
        c.shell,
        c.source,
        f"pytest {c.module}::test_dynamic",
        collect_only=collect_only,
        read=c.read,
    )
    assert report["transfer"]["size"] > 20_000
    assert len(report["collected"]) == 700
    assert report["collected"][-1] == f"{c.module}::test_dynamic[699]"
    assert report["executed"] == ([] if collect_only else report["collected"])


@pytest.mark.asyncio
async def test_oversized_pytest_report_fails_explicitly_without_partial_nodes(case):
    c = case
    c.parameter_count = 4_000
    with pytest.raises(build.BootstrapReadinessError, match="128 KB transfer bound"):
        await build._pytest_observation(
            c.shell, c.source, f"pytest {c.module}", collect_only=True, read=c.read
        )
    assert len(c.reads) == 1 and c.reads[0].endswith(".receipt.json")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["truncated", "wrong_identity", "wrong_exit", "timed_out"])
async def test_pytest_report_transfer_integrity_is_required(case, failure):
    c = case

    async def shell(command, timeout_sec):
        observed = json.loads(await c.shell(command, timeout_sec))
        if failure == "wrong_exit":
            observed["exit_code"] = 1
        if failure == "timed_out":
            observed["timed_out"] = True
        return json.dumps(observed)

    async def read(path):
        data = await c.read(path)
        if failure == "wrong_identity" and path.endswith(".receipt.json"):
            return json.dumps({**json.loads(data), "input_digest": "wrong"}).encode()
        if failure == "truncated" and not path.endswith(".receipt.json"):
            return data[1:]
        return data

    with pytest.raises(build.BootstrapReadinessError, match=r"differs|inconsistent|timed out"):
        await build._pytest_observation(
            shell, c.source, f"pytest {c.module}", collect_only=True, read=read
        )
