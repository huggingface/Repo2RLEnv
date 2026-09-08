from __future__ import annotations

import ast
import hashlib
import json
import shlex
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from repo2rlenv.tasksmith import build
from repo2rlenv.tasksmith.authoring import Design, Discovery
from repo2rlenv.tasksmith.config import TasksmithConfig
from repo2rlenv.tasksmith.emit import APT_STANZA
from repo2rlenv.tasksmith.readiness_repair import SmokeRepairError
from repo2rlenv.tasksmith.worker import save_json


@pytest.fixture
def setup(tmp_path, monkeypatch):
    source = {
        "id": "example-library-12",
        "repo": "example/library",
        "url": "https://github.com/example/library/pull/12",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }
    recipe = f"FROM python:3.12-slim@sha256:{'c' * 64}\n{APT_STANZA}\nRUN python -m pip install --no-cache-dir numpy==1.26.3\n"
    original = Discovery(
        useful_outcome="Preserve the complete public batching behavior.",
        behaviors=[
            {
                "id": "batching",
                "outcome": "Return the correct logical batch sequence.",
                "source_evidence": ["library/batches.py"],
            }
        ],
        dependency_dockerfile=recipe,
        dependency_inputs={"requirements.txt": "numpy==1.26.3"},
        readiness_commands=["python -c 'import library'"],
        upstream_test_commands=["python -m pytest tests/test_batches.py -q"],
        upstream_tests="relevant_tests",
        resource_rationale="Small explicit CPU fixtures exercise the public behavior.",
    )
    corrected = original.model_copy(
        update={
            "dependency_dockerfile": recipe.replace("1.26.3", "1.26.4"),
            "dependency_inputs": {"requirements.txt": "numpy==1.26.4"},
        }
    )
    config = TasksmithConfig(
        ledger_path=tmp_path / "budget.json", ledger_limit_usd=50, campaign_id="same-campaign"
    )
    s = SimpleNamespace(
        config=config,
        source=source,
        original=original,
        corrected=corrected,
        root=tmp_path / "construction",
        cache=tmp_path / "cache",
        now=1000,
        deadline=2000,
        failures=[True, False],
        events=[],
        workspaces=[],
        repair_calls=[],
        model_calls=0,
        interrupt_after_first=False,
        uncertain_cleanup=False,
        remote_files={},
        collection_failures={},
        collections=[],
        source_dirty=False,
        dirty_after_collection=False,
    )
    s.root.mkdir()
    monkeypatch.setattr(build.time, "time", lambda: s.now)

    async def prepare(remote, source, root):
        s.events.append(f"prepare-{remote.index}")
        receipt = {"source": source, "patch_digest": "d" * 64}
        save_json(root / "source-receipt.json", receipt)
        return receipt

    monkeypatch.setattr(build, "prepare_checked", prepare)

    async def dependency_diagnosis(*args, **kwargs):
        # These tests isolate the existing dependency rebuild policy. The real
        # independent smoke diagnosis and its evidence are tested separately.
        raise build.SmokeDependencyFailure("Independent assessment: dependency failure")

    monkeypatch.setattr(build, "correct_generated_smoke", dependency_diagnosis)

    @asynccontextmanager
    async def workspace(wc, root, budget, allowance):
        index = len(s.workspaces)
        s.workspaces.append((wc, root, budget, allowance))
        s.events.append(f"open-{index}")
        root.mkdir(parents=True, exist_ok=True)
        save_json(
            root / "resource.json",
            {"status": "running", "image_id": f"image-{index}", "resource_id": f"sandbox-{index}"},
        )

        async def shell(command, timeout_sec=120):
            s.events.append((index, command))
            if "tasksmith-dependency-source-check" in command:
                ast.parse(shlex.split(command)[-2])
                return json.dumps(
                    {"exit_code": 42 if s.source_dirty else 0, "stdout": "", "stderr": ""}
                )
            if "tasksmith-pytest-observation-capture" in command:
                ast.parse(shlex.split(command)[-2])
                capture = json.loads(shlex.split(command)[-1])
                module = capture["args"][0]
                s.collections.append((module, capture["args"], timeout_sec))
                if s.dirty_after_collection:
                    s.source_dirty = True
                failure = s.collection_failures.get(module)
                report = {
                    "exit_code": 2 if failure else 0,
                    "collected": [] if failure else [module + "::test_complete"],
                    "executed": [],
                    "stdout": failure or "",
                    "stderr": "",
                }
                data = json.dumps(report).encode()
                s.remote_files[capture["data_path"]] = data
                s.remote_files[capture["receipt_path"]] = json.dumps(
                    {
                        "input_digest": capture["input_digest"],
                        "status": "present",
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                ).encode()
                return json.dumps({"exit_code": report["exit_code"], "stdout": "", "stderr": ""})
            fails = s.failures[index] and "python -c 'import library'" in command
            return json.dumps(
                {
                    "exit_code": int(fails),
                    "stdout": "" if fails else "success",
                    "stderr": "ImportError: incompatible dependency API" if fails else "",
                }
            )

        async def read(path):
            return s.remote_files[path]

        remote = SimpleNamespace(index=index, shell=shell, read=read)
        try:
            yield remote
        finally:
            s.events.append(f"cleanup-{index}")
            save_json(
                root / "resource.json",
                {
                    "status": "uncertain" if s.uncertain_cleanup else "terminated",
                    "image_id": f"image-{index}",
                    "resource_id": f"sandbox-{index}",
                },
            )
            if index == 0 and s.interrupt_after_first:
                s.interrupt_after_first = False
                raise KeyboardInterrupt

    monkeypatch.setattr(build, "budgeted_workspace", workspace)

    async def artifact(**kwargs):
        s.repair_calls.append(kwargs)
        assert kwargs["schema"] is Discovery, "Design/construction must not precede valid readiness"
        path = kwargs["root"] / "artifact.json"
        if path.exists():
            return Discovery.model_validate(json.loads(path.read_text())["artifact"])
        s.model_calls += 1
        await kwargs["validate"](s.corrected)
        save_json(path, {"artifact": s.corrected.model_dump(mode="json")})
        return s.corrected

    monkeypatch.setattr(build, "artifact_stage", artifact)
    return s


@asynccontextmanager
async def ready(s):
    budget = s.config.budget(s.source["id"])
    async with build.bootstrap_ready(
        s.config, s.source, s.original, s.root, s.cache, s.deadline, budget
    ) as value:
        yield value


@pytest.mark.asyncio
async def test_dependency_repair_rebuilds_then_checks_reference_before_advancing(setup):
    s = setup
    original = s.original.model_dump()
    async with ready(s) as (discovered, wc, readiness, remote):
        assert discovered == s.corrected and readiness["passed"]
        assert remote.index == 1
        assert wc.deadline == s.deadline
    assert s.original.model_dump() == original
    assert len(s.workspaces) == 2 and s.model_calls == 1
    assert s.events.index("cleanup-0") < s.events.index("open-1")
    assert s.workspaces[0][0].deadline == s.workspaces[1][0].deadline
    assert s.workspaces[0][2] is s.workspaces[1][2] is s.repair_calls[0]["budget"]
    assert s.repair_calls[0]["root"].name == "bootstrap-repair-1"
    assert s.repair_calls[0]["inputs"]["failed_readiness"]["checks"][-1]["stderr"].startswith(
        "ImportError"
    )
    assert json.loads((s.root / "readiness.json").read_text())["passed"] is False
    assert json.loads((s.root / "bootstrap-attempt-2/readiness.json").read_text())["passed"] is True
    for index in range(2):
        commands = [
            event[1] for event in s.events if isinstance(event, tuple) and event[0] == index
        ]
        assert s.source["head_sha"] in commands[0]
        assert "--no-index --no-deps --no-build-isolation" in commands[0]
        assert s.source["base_sha"] in commands[-1]
    cache = build.BootstrapCache(s.cache)
    assert (
        cache.lookup(
            cache.identity(s.original.dependency_dockerfile, s.original.dependency_inputs), "modal"
        )
        is None
    )
    repaired = cache.lookup(
        cache.identity(s.corrected.dependency_dockerfile, s.corrected.dependency_inputs), "modal"
    )
    assert repaired["recipe"] == s.corrected.dependency_dockerfile
    assert repaired["inputs"] == s.corrected.dependency_inputs and repaired["image_id"] == "image-1"


@pytest.mark.asyncio
async def test_successful_initial_readiness_needs_no_repair_or_second_workspace(setup):
    s = setup
    s.failures = [False]
    async with ready(s) as (discovered, _, readiness, _):
        assert discovered == s.original and readiness["passed"]
    assert len(s.workspaces) == 1 and not s.repair_calls


@pytest.mark.asyncio
async def test_two_failed_attempts_never_enter_design_or_construction(setup):
    s = setup
    s.failures = [True, True]
    with pytest.raises(build.BootstrapReadinessError, match="after two bootstrap attempts"):
        await build.construct(
            s.config,
            s.source,
            {"artifact": s.original.model_dump(), "source_receipt": {}},
            s.root,
            s.cache,
            s.deadline,
        )
    assert len(s.workspaces) == 2 and s.model_calls == 1
    assert all(call["schema"] is Discovery for call in s.repair_calls)
    assert not (s.root / "construction.json").exists()
    assert not list(s.cache.glob("*/modal.json"))
    assert s.events[-1] == "cleanup-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change, message",
    [
        ("same", "must change"),
        ("base", "immutable Python base"),
        ("checks", "cannot weaken"),
        ("target", "target published package"),
    ],
)
async def test_invalid_dependency_correction_cannot_weaken_scope_or_skip_readiness(
    setup, change, message
):
    s = setup
    if change == "same":
        s.corrected = s.original
    elif change == "base":
        s.corrected = s.corrected.model_copy(
            update={
                "dependency_dockerfile": s.corrected.dependency_dockerfile.replace(
                    "c" * 64, "e" * 64
                )
            }
        )
    elif change == "checks":
        s.corrected = s.corrected.model_copy(update={"readiness_commands": ["true"]})
    else:
        s.corrected = s.corrected.model_copy(
            update={
                "dependency_dockerfile": s.corrected.dependency_dockerfile
                + "RUN python -m pip install --no-cache-dir library==1.0\n"
            }
        )
    with pytest.raises(ValueError, match=message):
        async with ready(s):
            pytest.fail("Invalid correction advanced")
    assert len(s.workspaces) == 1 and s.events[-1] == "cleanup-0"


@pytest.mark.asyncio
async def test_resume_uses_retained_correction_without_reopening_failed_workspace_or_rerolling(
    setup,
):
    s = setup
    s.interrupt_after_first = True
    with pytest.raises(KeyboardInterrupt):
        async with ready(s):
            pytest.fail("Interrupted first attempt advanced")
    failed_bytes = (s.root / "readiness.json").read_bytes()
    correction_bytes = (s.root / "bootstrap-repair-1/artifact.json").read_bytes()
    async with ready(s) as (_, _, readiness, remote):
        assert readiness["passed"] and remote.index == 1
    assert s.model_calls == 1
    assert len(s.workspaces) == 2
    assert s.repair_calls[-1]["shell"] is None
    assert (s.root / "readiness.json").read_bytes() == failed_bytes
    assert (s.root / "bootstrap-repair-1/artifact.json").read_bytes() == correction_bytes


@pytest.mark.asyncio
async def test_unconfirmed_cleanup_blocks_second_build(setup):
    s = setup
    s.uncertain_cleanup = True
    with pytest.raises(build.BootstrapReadinessError, match="cleanup is unconfirmed"):
        async with ready(s):
            pytest.fail("Uncertain cleanup advanced")
    assert len(s.workspaces) == 1
    with pytest.raises(build.BootstrapReadinessError, match="cleanup is unconfirmed"):
        async with ready(s):
            pytest.fail("Uncertain cleanup resumed")
    assert len(s.workspaces) == 1 and s.model_calls == 1


@pytest.mark.asyncio
async def test_deadline_is_not_extended_for_retry_or_resume(setup):
    s = setup
    s.interrupt_after_first = True
    with pytest.raises(KeyboardInterrupt):
        async with ready(s):
            pytest.fail("Interrupted first attempt advanced")
    s.now = 2001
    with pytest.raises(TimeoutError, match="Original bootstrap deadline"):
        async with ready(s):
            pytest.fail("Expired bootstrap advanced")
    assert len(s.workspaces) == 1
    s.deadline = 3000
    with pytest.raises(build.BootstrapReadinessError, match="deadline changed"):
        async with ready(s):
            pytest.fail("Reset deadline was accepted")


@pytest.mark.asyncio
async def test_construct_design_receives_only_repaired_successful_readiness(setup, monkeypatch):
    s = setup
    artifact = build.artifact_stage

    class AtDesign(Exception):
        pass

    async def stage(**kwargs):
        if kwargs["schema"] is Design:
            assert kwargs["inputs"]["bootstrap"]["passed"]
            assert (
                kwargs["inputs"]["discovery"]["dependency_dockerfile"]
                == s.corrected.dependency_dockerfile
            )
            raise AtDesign
        return await artifact(**kwargs)

    monkeypatch.setattr(build, "artifact_stage", stage)
    with pytest.raises(AtDesign):
        await build.construct(
            s.config,
            s.source,
            {"artifact": s.original.model_dump(), "source_receipt": {}},
            s.root,
            s.cache,
            s.deadline,
        )
    assert len(s.workspaces) == 2 and s.events[-1] == "cleanup-1"


@pytest.mark.asyncio
async def test_approved_generated_smoke_reruns_complete_readiness_in_same_workspace(
    setup, monkeypatch
):
    s = setup
    s.original = s.original.model_copy(
        update={
            "readiness_commands": [
                "python -c 'import sys'",
                *s.original.readiness_commands,
                "python -c 'assert 2 + 2 == 4'",
            ],
        }
    )
    replacement = "python -c 'import library; assert library.correct_api()'"
    corrected = s.original.model_copy(
        update={
            "readiness_commands": [
                s.original.readiness_commands[0],
                replacement,
                s.original.readiness_commands[-1],
            ],
        }
    )
    calls = []

    async def approve(config, source, discovered, readiness, root, **kwargs):
        assert discovered == s.original and readiness["checks"][-1]["exit_code"] == 1
        assert config is s.config and source == s.source
        if (root / "phase.json").exists():
            with pytest.raises(build.BootstrapReadinessError, match="cannot execute remote"):
                await kwargs["shell"]("any command")
            return corrected
        assert kwargs["deadline"] == s.deadline and kwargs["budget"] is s.workspaces[0][2]
        assert kwargs["shell"] is not None
        calls.append(root)
        save_json(root / "phase.json", {"status": "completed"})
        save_json(
            root / "result.json",
            {
                "input_digest": "a" * 64,
                "assessment": {"approved": True, "classification": "invalid_generated_smoke"},
            },
        )
        return corrected

    monkeypatch.setattr(build, "correct_generated_smoke", approve)
    async with ready(s) as (discovered, _, readiness, remote):
        assert discovered == corrected and readiness["passed"] and remote.index == 0
    assert len(calls) == len(s.workspaces) == 1 and not s.repair_calls
    old = json.loads((s.root / "readiness.json").read_text())
    new = json.loads((s.root / "generated-smoke-readiness-1/readiness.json").read_text())
    assert old["passed"] is False and new["passed"] is True
    assert [row["command"] for row in new["checks"]][1:] == [
        *corrected.readiness_commands,
        *s.original.upstream_test_commands,
    ]
    assert s.source["head_sha"] in new["checks"][0]["command"]
    assert old["cache_key"] == new["cache_key"]
    proof = calls[0] / "result.json"
    assert new["smoke_proof"]["sha256"] == hashlib.sha256(proof.read_bytes()).hexdigest()
    assert new["smoke_proof"]["assessment"]["approved"] is True
    for field in Discovery.model_fields:
        if field != "readiness_commands":
            assert getattr(corrected, field) == getattr(s.original, field)
    # A terminal workspace cannot be silently reopened to repeat an approved phase.
    before = (s.root / "readiness.json").read_bytes()
    with pytest.raises(build.BootstrapReadinessError, match="explicit workspace reconciliation"):
        async with ready(s):
            pytest.fail("Approved old smoke was reopened")
    assert len(s.workspaces) == 1 and (s.root / "readiness.json").read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["unknown", "reference_failure", "incomplete"])
async def test_unapproved_smoke_diagnosis_cannot_fallback_or_reroll(setup, monkeypatch, reason):
    s = setup
    paid_diagnoses = []

    async def reject(config, source, discovered, readiness, root, **kwargs):
        phase = root / "phase.json"
        if not phase.exists():
            paid_diagnoses.append(reason)
            save_json(phase, {"status": "incomplete" if reason == "incomplete" else "completed"})
            if reason != "incomplete":
                save_json(root / "result.json", {"classification": reason})
        raise SmokeRepairError("Retained smoke diagnosis: " + reason)

    monkeypatch.setattr(build, "correct_generated_smoke", reject)
    for _ in range(2):
        with pytest.raises((SmokeRepairError, build.BootstrapReadinessError), match=reason):
            async with ready(s):
                pytest.fail("Unapproved command correction advanced")
    assert len(s.workspaces) == len(paid_diagnoses) == 1
    assert not s.repair_calls and not (s.root / "construction.json").exists()
    assert not (s.root / "generated-smoke-readiness-1").exists()


@pytest.mark.asyncio
async def test_retained_dependency_diagnosis_preserves_enabled_identity_without_remote_effects(
    setup, monkeypatch
):
    s = setup
    s.interrupt_after_first = True
    fresh = []

    async def diagnose(config, source, discovered, readiness, root, **kwargs):
        phase = root / "phase.json"
        assert kwargs["shell"] is not None
        if phase.exists():
            with pytest.raises(build.BootstrapReadinessError, match="cannot execute remote"):
                await kwargs["shell"]("must refuse")
        else:
            fresh.append(root)
            save_json(phase, {"status": "completed"})
            save_json(root / "result.json", {"classification": "dependency_failure"})
        raise build.SmokeDependencyFailure("Independently classified dependency failure")

    monkeypatch.setattr(build, "correct_generated_smoke", diagnose)
    with pytest.raises(KeyboardInterrupt):
        async with ready(s):
            pytest.fail("Interrupted first image advanced")
    async with ready(s) as (_, _, readiness, remote):
        assert readiness["passed"] and remote.index == 1
    assert len(fresh) == s.model_calls == 1 and len(s.workspaces) == 2


@pytest.mark.asyncio
async def test_approved_smoke_that_still_fails_cannot_trigger_more_command_or_dependency_repair(
    setup, monkeypatch
):
    s = setup
    calls = []

    async def approve(config, source, discovered, readiness, root, **kwargs):
        calls.append(root)
        save_json(
            root / "result.json",
            {
                "input_digest": "a" * 64,
                "assessment": {"approved": True, "classification": "invalid_generated_smoke"},
            },
        )
        return discovered.model_copy(
            update={
                "readiness_commands": [discovered.readiness_commands[0] + " # still fails"],
            }
        )

    monkeypatch.setattr(build, "correct_generated_smoke", approve)
    with pytest.raises(
        build.BootstrapReadinessError, match="Corrected generated smoke still failed"
    ):
        async with ready(s):
            pytest.fail("Failed corrected command advanced")
    assert len(calls) == len(s.workspaces) == 1 and not s.repair_calls
    assert not json.loads((s.root / "generated-smoke-readiness-1/readiness.json").read_text())[
        "passed"
    ]


@pytest.mark.asyncio
async def test_upstream_scipy_import_failure_routes_only_to_dependency_repair(setup, monkeypatch):
    s = setup
    s.failures = [False, False]
    workspace = build.budgeted_workspace

    @asynccontextmanager
    async def failing_upstream(*args):
        async with workspace(*args) as remote:
            shell = remote.shell

            async def upstream_shell(command, timeout_sec=120):
                if remote.index == 0 and s.original.upstream_test_commands[0] in command:
                    return json.dumps(
                        {
                            "exit_code": 4,
                            "stdout": "ERROR collecting tests/test_initialization.py\n"
                            "ImportError while importing test module\nfrom scipy import stats\n"
                            "E ModuleNotFoundError: No module named 'scipy'",
                            "stderr": "ERROR: found no collectors for tests/test_initialization.py::test_init",
                        }
                    )
                return await shell(command, timeout_sec)

            remote.shell = upstream_shell
            yield remote

    async def unexpected_smoke(*args, **kwargs):
        pytest.fail("Upstream failure entered generated-smoke repair")

    monkeypatch.setattr(build, "budgeted_workspace", failing_upstream)
    monkeypatch.setattr(build, "correct_generated_smoke", unexpected_smoke)
    async with ready(s) as (discovered, _, readiness, remote):
        assert readiness["passed"] and discovered == s.corrected and remote.index == 1
    assert len(s.repair_calls) == 1
    assert s.repair_calls[0]["stage"] == "bootstrap-repair-1"
    assert not (s.root / "bootstrap-selector-repair-1").exists()


@pytest.mark.parametrize(
    "message",
    [
        "E   ValueError: Your setup doesn't support bf16/gpu.",
        "RuntimeError: Found no NVIDIA driver on your system. Please check that you have an NVIDIA GPU.",
        "AssertionError: Torch not compiled with CUDA enabled",
    ],
)
def test_observed_gpu_requirement_is_a_cpu_profile_failure(setup, message):
    s = setup
    evidence = {
        "passed": False,
        "checks": [
            {
                "command": s.original.upstream_test_commands[0],
                "exit_code": 1,
                "stdout": message,
            }
        ],
    }
    failure = build._profile_failure(s.original, evidence)
    assert failure["profile"] == "cpu-direct" and failure["exception"] == message


@pytest.mark.parametrize(
    "output,code,passed",
    [
        ('    raise ValueError("Your setup doesn\'t support bf16/gpu.")', 1, False),
        ("GPU warning: bf16 unavailable; continuing on CPU", 1, False),
        ("E   ModuleNotFoundError: No module named 'attr'", 4, False),
        ("ValueError: Your setup doesn't support bf16/gpu.", 0, True),
    ],
)
def test_profile_classifier_requires_a_failed_capability_exception(setup, output, code, passed):
    s = setup
    assert (
        build._profile_failure(
            s.original,
            {
                "passed": passed,
                "checks": [
                    {
                        "command": s.original.upstream_test_commands[0],
                        "exit_code": code,
                        "stdout": output,
                    }
                ],
            },
        )
        is None
    )


@pytest.mark.asyncio
async def test_profile_failure_preserves_checks_and_stops_before_any_repair_or_rebuild(
    setup, monkeypatch
):
    s = setup
    s.failures = [False]
    workspace = build.budgeted_workspace

    @asynccontextmanager
    async def gpu_test(*args):
        async with workspace(*args) as remote:
            shell = remote.shell

            async def gpu_shell(command, timeout_sec=120):
                if s.original.upstream_test_commands[0] in command:
                    return json.dumps(
                        {
                            "exit_code": 1,
                            "stdout": "tests/test_batches.py:226: in test_evaluate\n"
                            "    config = DPOConfig()\n"
                            "training_args.py:1739: ValueError\n"
                            "E   ValueError: Your setup doesn't support bf16/gpu.\n",
                            "stderr": "",
                        }
                    )
                return await shell(command, timeout_sec)

            remote.shell = gpu_shell
            yield remote

    async def no_repair(*args, **kwargs):
        pytest.fail("Profile mismatch entered model repair")

    monkeypatch.setattr(build, "budgeted_workspace", gpu_test)
    monkeypatch.setattr(build, "correct_generated_smoke", no_repair)
    monkeypatch.setattr(build, "artifact_stage", no_repair)
    with pytest.raises(build.BootstrapReadinessError, match="CPU profile/configuration mismatch"):
        async with ready(s):
            pytest.fail("GPU-only invocation advanced")
    old = (s.root / "readiness.json").read_bytes()
    mismatch = json.loads((s.root / "profile-mismatch.json").read_text())
    assert mismatch["readiness_digest"] == build.canonical_digest(json.loads(old))
    assert mismatch["source"]["head_sha"] == s.source["head_sha"]
    with pytest.raises(build.BootstrapReadinessError, match="CPU profile/configuration mismatch"):
        async with ready(s):
            pytest.fail("Retained profile failure rerolled")
    assert (s.root / "readiness.json").read_bytes() == old
    assert len(s.workspaces) == 1 and not s.collections


@pytest.fixture
def cpu_setup(setup, monkeypatch):
    """Captured source and reports are synthetic data; no TRL/pytest target runs."""
    s = setup
    s.source.update(repo="huggingface/trl", url="https://github.com/huggingface/trl/pull/6206")
    s.failures = [False]
    s.cpu_reviews = []
    s.cpu_approved = True
    s.cpu_incomplete = False
    s.cpu_exit_code = 0
    s.cpu_zero_execution = False
    s.cpu_missing_anchor = False
    s.cpu_bad_source = False
    s.cpu_bad_transfer = False
    s.cpu_capture_error = False
    s.cpu_reports = []
    s.installed_version = "4.57.6"
    s.version_result = None
    s.cpu_sources = {
        "tests/test_batches.py": "from trl import DPOConfig\nfrom .helpers import config_fixture\n"
        "def test_complete():\n    args = DPOConfig(output_dir='tmp')\n    assert args.seed == 42\n",
        "tests/helpers.py": "def config_fixture():\n    return 'unchanged fixture'\n",
        "tests/__init__.py": "",
        "trl/trainer/base_config.py": "class BaseConfig:\n    bf16 = True\n",
        "trl/trainer/dpo_config.py": "class DPOConfig:\n    use_cpu = False\n    bf16 = True\n",
    }
    s.before = "def test_complete():\n    assert True\n"
    workspace = build.budgeted_workspace

    @asynccontextmanager
    async def cpu_workspace(*args):
        async with workspace(*args) as remote:
            shell = remote.shell

            async def cpu_shell(command, timeout_sec=120):
                if command == build.cpu_fixture.VERSION_COMMAND:
                    assert not s.cpu_reviews
                    return s.version_result or json.dumps(
                        {
                            "exit_code": 0,
                            "stdout": json.dumps(
                                {"distribution": "transformers", "version": s.installed_version}
                            ),
                            "stderr": "",
                        }
                    )
                if "tasksmith-selector-source-capture" in command:
                    ast.parse(shlex.split(command)[-2])
                    capture = json.loads(shlex.split(command)[-1])
                    text = s.cpu_sources.get(capture["module"])
                    if capture["commit"] == s.source["base_sha"]:
                        text = s.before
                    data = b"" if text is None else text.encode()
                    receipt = {
                        "commit": capture["commit"],
                        "module": capture["module"],
                        "status": "present" if text is not None else "missing",
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                    if len(data) > capture["limit"] or s.cpu_capture_error:
                        receipt.update(
                            status="error", reason="Pinned source transfer exceeds bound"
                        )
                    s.remote_files[capture["receipt_path"]] = json.dumps(receipt).encode()
                    s.remote_files[capture["data_path"]] = data
                    return json.dumps(
                        {"exit_code": 0, "stdout": data[-20_000:].decode(), "stderr": ""}
                    )
                if "tasksmith-pytest-observation-capture" in command:
                    ast.parse(shlex.split(command)[-2])
                    capture = json.loads(shlex.split(command)[-1])
                    assert "cpu_fixture" in capture
                    s.cpu_reports.append(capture)
                    collected = "--collect-only" in capture["args"]
                    nodes = ["tests/test_batches.py::test_complete"]
                    # Distinguish first full collection from subsequent selected runs.
                    if (
                        s.cpu_missing_anchor
                        and capture["args"][0].startswith("tests/")
                        and len(s.cpu_reports) > 1
                    ):
                        nodes = ["tests/test_batches.py::test_other"]
                    code = 0 if collected else s.cpu_exit_code
                    report = {
                        "exit_code": code,
                        "collected": nodes,
                        "executed": [] if collected or s.cpu_zero_execution else nodes,
                        "stdout": "",
                        "stderr": "",
                        "cpu_fixture": {
                            "input_digest": capture["cpu_fixture"]["input_digest"],
                            "argv": capture["args"],
                            "source_unchanged": not s.cpu_bad_source,
                            "refused": [],
                            "calls": {"DPOConfig": {"injected": 1}},
                            "exit_code": code,
                        },
                    }
                    data = json.dumps(report).encode()
                    s.remote_files[capture["receipt_path"]] = json.dumps(
                        {
                            "input_digest": capture["input_digest"],
                            "status": "present",
                            "size": len(data),
                            "sha256": hashlib.sha256(data).hexdigest(),
                        }
                    ).encode()
                    s.remote_files[capture["data_path"]] = data + (
                        b"corrupt" if s.cpu_bad_transfer else b""
                    )
                    return json.dumps({"exit_code": code, "stdout": "tail only", "stderr": ""})
                if s.original.upstream_test_commands[0] in command:
                    return json.dumps(
                        {
                            "exit_code": 1,
                            "stdout": "",
                            "stderr": "ValueError: Your setup doesn't support bf16/gpu.",
                        }
                    )
                return await shell(command, timeout_sec)

            remote.shell = cpu_shell
            yield remote

    monkeypatch.setattr(build, "budgeted_workspace", cpu_workspace)

    async def assess(budget, model, messages, **kwargs):
        from tests.test_tasksmith_cpu_fixture import delivered_evidence, structured_response

        inputs = json.loads((s.root / "bootstrap-cpu-fixture-1/approval/inputs.json").read_text())
        evidence = build.cpu_fixture.restore_dossier(
            delivered_evidence(messages[1]["content"]), inputs["projection_receipt"]
        )
        s.cpu_reviews.append(
            dict(budget=budget, model=model, messages=messages, evidence=evidence, **kwargs)
        )
        if s.cpu_incomplete:
            raise TimeoutError("Independent review interrupted")
        request = json.loads(evidence["request"])
        source_quote = build.cpu_fixture.Citation(
            evidence_id="source/tests/test_batches.py", quote="args = DPOConfig(output_dir='tmp')"
        )
        result = build.cpu_fixture.CpuFixtureAssessment(
            approved=s.cpu_approved,
            essential_gpu_semantics=not s.cpu_approved,
            explanation="The complete fixture checks CPU-compatible behavior without GPU claims.",
            commands=[
                {
                    "command": command,
                    "cpu_semantics_preserved": s.cpu_approved,
                    "explanation": "This command retains its complete source assertions on CPU.",
                    "evidence": [source_quote],
                }
                for command in request["discovery"]["upstream_test_commands"]
            ],
            evidence=[
                source_quote,
                {"evidence_id": "failure", "quote": "Your setup doesn't support bf16/gpu."},
            ],
        )
        return structured_response(result), 0.0

    monkeypatch.setattr(build.cpu_fixture, "completion", assess)
    return s


@pytest.mark.asyncio
async def test_cpu_adaptation_reviews_source_then_runs_full_checks_same_image(cpu_setup):
    s = cpu_setup
    original = s.original.model_dump(mode="json")
    async with ready(s) as (discovered, wc, evidence, remote):
        assert evidence["passed"] and remote.index == 0
        assert discovered.model_dump(mode="json") == original
        inputs = json.loads((s.root / "bootstrap-cpu-fixture-1/approval/inputs.json").read_text())
        assert inputs["deadline"] == wc.deadline == s.deadline
        assert inputs["request"]["transformers_version"] == "4.57.6"
        assert s.cpu_reviews[0]["budget"] is s.workspaces[0][2]
        assert "source/tests/helpers.py" in s.cpu_reviews[0]["evidence"]
        assert "source/tests/__init__.py" in s.cpu_reviews[0]["evidence"]
        assert [row["command"] for row in evidence["checks"]] == build._readiness_inputs(
            s.source, s.original, evidence["cache_key"]
        )["commands"]
        assert evidence["selector_proof"]["required_changed_anchors"] == [
            "tests/test_batches.py::test_complete"
        ]
        assert evidence["checks"][-1]["executed"] == ["tests/test_batches.py::test_complete"]
        assert evidence["cpu_fixture_proof"]["assessment"]["approved"] is True
    assert len(s.workspaces) == len(s.cpu_reviews) == 1 and not s.repair_calls
    assert len(s.cpu_reports) == 3  # Full modules, original selection, actual execution.
    assert all(c["cpu_fixture"]["head_sha"] == s.source["head_sha"] for c in s.cpu_reports)
    initial = json.loads((s.root / "readiness.json").read_text())
    assert initial["passed"] is False and "bf16/gpu" in initial["checks"][-1]["stderr"]
    assert (
        len([e for e in s.events if isinstance(e, tuple) and "python -c 'import library'" in e[1]])
        == 2
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    ["rejected", "incomplete", "failed", "zero", "missing_anchor", "source", "transfer", "capture"],
)
async def test_cpu_adaptation_failure_never_rebuilds_or_rerolls(cpu_setup, mode):
    s = cpu_setup
    if mode == "rejected":
        s.cpu_approved = False
    elif mode == "incomplete":
        s.cpu_incomplete = True
    elif mode == "failed":
        s.cpu_exit_code = 1
    elif mode == "zero":
        s.cpu_zero_execution = True
    elif mode == "missing_anchor":
        s.cpu_missing_anchor = True
    elif mode == "source":
        s.cpu_bad_source = True
    elif mode == "transfer":
        s.cpu_bad_transfer = True
    else:
        s.cpu_capture_error = True
    with pytest.raises(
        (
            build.BootstrapReadinessError,
            build.cpu_fixture.CpuFixtureUnsupported,
            TimeoutError,
            ValueError,
        )
    ):
        async with ready(s):
            pytest.fail("Unproven adapted readiness advanced")
    assert len(s.workspaces) == 1 and not s.repair_calls
    before = {str(p.relative_to(s.root)): p.read_bytes() for p in s.root.rglob("*") if p.is_file()}
    with pytest.raises(build.BootstrapReadinessError, match="Retained CPU fixture"):
        async with ready(s):
            pytest.fail("Persisted CPU attempt restarted")
    assert len(s.workspaces) == 1 and len(s.cpu_reviews) <= 1
    assert before == {
        str(p.relative_to(s.root)): p.read_bytes() for p in s.root.rglob("*") if p.is_file()
    }
    if mode == "source":
        observations = list((s.root / "bootstrap-cpu-fixture-1/observations").glob("*.json"))
        assert (
            observations
            and json.loads(observations[0].read_text())["report"]["cpu_fixture"]["source_unchanged"]
            is False
        )


@pytest.mark.asyncio
async def test_cpu_source_capture_does_not_read_shell_tail(cpu_setup):
    s = cpu_setup
    s.cpu_sources["tests/test_batches.py"] += "# trusted synthetic padding\n" * 1000
    async with ready(s):
        assert (
            s.cpu_reviews[0]["evidence"]["source/tests/test_batches.py"]
            == s.cpu_sources["tests/test_batches.py"]
        )
    assert not s.repair_calls


@pytest.mark.asyncio
async def test_dependency_submission_requires_all_modules_not_just_first_missing_package(
    setup, monkeypatch
):
    s = setup
    commands = [
        "pytest tests/test_batches.py::test_selected -k selected -q",
        "pytest tests/test_serialization.py::TestState::test_round_trip --no-header -q",
    ]
    s.original = s.original.model_copy(update={"upstream_test_commands": commands})
    s.corrected = s.corrected.model_copy(update={"upstream_test_commands": commands})
    s.collection_failures["tests/test_serialization.py"] = (
        "E ModuleNotFoundError: No module named 'attr'"
    )

    async def repairing_phase(**kwargs):
        s.repair_calls.append(kwargs)
        assert kwargs["inputs"]["dependency_collection_policy"] == 1
        with pytest.raises(ValueError, match="No module named 'attr'"):
            await kwargs["validate"](s.corrected)
        assert len(s.workspaces) == 1
        failed = list(kwargs["root"].glob("dependency-collection/*/attempt-0.json"))
        assert len(failed) == 1
        prior_bytes = failed[0].read_bytes()
        proof = json.loads(prior_bytes)
        assert proof["passed"] is False and proof["reports"][-1]["exit_code"] == 2
        # Model a diagnostic install in the existing remote environment. The
        # final recipe includes that same pin; no target code executes locally.
        s.collection_failures.clear()
        s.corrected = s.corrected.model_copy(
            update={
                "dependency_dockerfile": s.corrected.dependency_dockerfile
                + "RUN python -m pip install --no-cache-dir attrs==25.3.0\n",
            }
        )
        await kwargs["validate"](s.corrected)
        assert failed[0].read_bytes() == prior_bytes
        save_json(kwargs["root"] / "artifact.json", {"artifact": s.corrected.model_dump()})
        return s.corrected

    monkeypatch.setattr(build, "artifact_stage", repairing_phase)
    async with ready(s) as (discovered, _, readiness, remote):
        assert readiness["passed"] and remote.index == 1 and discovered == s.corrected
    assert len(s.workspaces) == 2 and len(s.repair_calls) == 1
    assert [module for module, _, _ in s.collections] == [
        "tests/test_batches.py",
        "tests/test_serialization.py",
        "tests/test_batches.py",
        "tests/test_serialization.py",
    ]
    assert all(args == [module, "-q", "--collect-only"] for module, args, _ in s.collections)
    assert discovered.upstream_test_commands == commands
    retained_proofs = list(
        (s.root / "bootstrap-repair-1").glob("dependency-collection/*/proof.json")
    )
    assert (
        len(retained_proofs) == 1
        and json.loads(retained_proofs[0].read_text())["fresh_image_required"]
    )


@pytest.mark.asyncio
async def test_source_changes_cannot_supply_dependency_collection_proof(setup):
    s = setup
    s.source_dirty = True
    with pytest.raises(ValueError, match="changed source/tests"):
        async with ready(s):
            pytest.fail("Modified source was accepted as a dependency fix")
    assert len(s.workspaces) == 1 and not s.collections
    evidence = next((s.root / "bootstrap-repair-1").glob("dependency-collection/*/attempt-0.json"))
    assert json.loads(evidence.read_text())["source_check"]["exit_code"] == 42


@pytest.mark.asyncio
async def test_collection_cannot_change_source_then_supply_successful_proof(setup):
    s = setup
    s.dirty_after_collection = True
    with pytest.raises(ValueError, match="collection changed the pinned source/tests"):
        async with ready(s):
            pytest.fail("Collection-time source mutation advanced")
    record = next((s.root / "bootstrap-repair-1").glob("dependency-collection/*/attempt-0.json"))
    evidence = json.loads(record.read_text())
    assert evidence["source_check"]["exit_code"] == 0
    assert evidence["source_after"]["exit_code"] == 42 and not evidence["passed"]
    assert evidence["reset"]["exit_code"] == 0 and len(s.workspaces) == 1


@pytest.mark.asyncio
async def test_dependency_proof_missing_on_resume_does_not_rebuild_or_repeat_diagnosis(setup):
    s = setup
    s.interrupt_after_first = True
    with pytest.raises(KeyboardInterrupt):
        async with ready(s):
            pytest.fail("Interrupted bootstrap advanced")
    proof = next((s.root / "bootstrap-repair-1").glob("dependency-collection/*/proof.json"))
    proof.unlink()  # Tamper only with this test's temporary fixture.
    count = len(s.collections)
    with pytest.raises(
        build.BootstrapReadinessError, match="lacks complete retained module collection"
    ):
        async with ready(s):
            pytest.fail("Missing collection proof was ignored")
    assert len(s.workspaces) == s.model_calls == 1 and len(s.collections) == count


@pytest.mark.asyncio
async def test_dependency_collection_obeys_original_deadline(setup):
    s = setup

    async def no_effect(*args, **kwargs):
        pytest.fail("Expired collection executed a remote effect")

    with pytest.raises(TimeoutError, match="Original dependency diagnosis deadline"):
        await build._dependency_collection(
            no_effect,
            no_effect,
            s.source,
            s.corrected,
            s.root,
            s.now,
        )


@pytest.mark.asyncio
async def test_gpu_failure_during_full_module_collection_stops_dependency_phase(setup):
    s = setup
    s.collection_failures["tests/test_batches.py"] = "RuntimeError: No CUDA GPUs are available"
    with pytest.raises(build.BootstrapReadinessError, match="CPU profile/configuration mismatch"):
        async with ready(s):
            pytest.fail("GPU-only module accepted as missing dependency")
    assert len(s.workspaces) == 1
    assert list(
        (s.root / "bootstrap-repair-1").glob("dependency-collection/*/profile-mismatch.json")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unsupported", "nonzero", "timeout", "truncated", "oversized"])
async def test_cpu_installed_version_is_captured_before_review_and_failure_is_terminal(
    cpu_setup, mode
):
    s = cpu_setup
    if mode == "unsupported":
        s.installed_version = "4.99.0"
    else:
        result = {
            "exit_code": 0,
            "stdout": json.dumps({"distribution": "transformers", "version": "4.57.6"}),
        }
        if mode == "nonzero":
            result["exit_code"] = 1
        elif mode == "timeout":
            result["timed_out"] = True
        elif mode == "truncated":
            result["stdout"] = '{"version":'
        else:
            result["stdout"] = "x" * 4100
        s.version_result = json.dumps(result)
    with pytest.raises(ValueError):
        async with ready(s):
            pytest.fail("Unknown image version advanced")
    assert not s.cpu_reviews and not s.repair_calls and len(s.workspaces) == 1
    receipt = s.root / "bootstrap-cpu-fixture-1/installed-transformers.json"
    assert receipt.exists()
    before = receipt.read_bytes()
    with pytest.raises(build.BootstrapReadinessError, match="Retained CPU fixture"):
        async with ready(s):
            pytest.fail("Invalid version capture retried")
    assert receipt.read_bytes() == before and not s.cpu_reviews
