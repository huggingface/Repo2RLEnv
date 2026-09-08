from __future__ import annotations

import hashlib
import json
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
            fails = s.failures[index] and "python -c 'import library'" in command
            return json.dumps(
                {
                    "exit_code": int(fails),
                    "stdout": "" if fails else "success",
                    "stderr": "ImportError: incompatible dependency API" if fails else "",
                }
            )

        remote = SimpleNamespace(index=index, shell=shell)
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
