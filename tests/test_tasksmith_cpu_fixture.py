from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from repo2rlenv.tasksmith import cpu_fixture as cpu
from repo2rlenv.tasksmith.authoring import Discovery
from repo2rlenv.tasksmith.config import TasksmithConfig


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """All source texts/classes in this file are trusted synthetic fixtures."""
    config = TasksmithConfig(
        ledger_path=tmp_path / "budget.json", ledger_limit_usd=50, campaign_id="cpu-fixture-test"
    )
    discovery = Discovery(
        useful_outcome="Cache identical preprocessing inputs without repeated work.",
        behaviors=[
            {
                "id": "cache",
                "outcome": "Identical preprocessing inputs reuse cached outputs.",
                "source_evidence": ["trl/trainer/dpo_config.py"],
            }
        ],
        dependency_dockerfile="FROM python:3.12-slim@sha256:" + "a" * 64,
        dependency_inputs={"requirements.txt": "transformers==4.56.2\n"},
        readiness_commands=["python -c 'import trl'"],
        upstream_test_commands=["pytest tests/test_cpu.py -k 'not slow' -v"],
        upstream_tests="relevant_tests",
        resource_rationale="Tiny inputs exercise identical cache semantics on CPU.",
    )
    texts = {
        "tests/test_cpu.py": "def test_cpu():\n    args = DPOConfig(output_dir='tmp')\n    assert args.seed == 42\n",
        "trl/trainer/dpo_config.py": "class DPOConfig:\n    use_cpu = False\n    bf16 = True\n",
        "trl/trainer/base_config.py": "class BaseConfig:\n    bf16 = True\n",
    }
    request = cpu.CpuFixtureRequest(
        source={"repository": "huggingface/trl", "number": 6206},
        base_sha="a" * 40,
        head_sha="b" * 40,
        discovery=discovery,
        constructors=["DPOConfig"],
        source_files=[
            {
                "path": path,
                "text": text,
                "size": len(text.encode()),
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
            for path, text in texts.items()
        ],
        readiness={
            "passed": False,
            "reset": {"exit_code": 0},
            "checks": [
                {
                    "command": "git checkout --detach "
                    + "b" * 40
                    + " && python -m pip install --no-index --no-deps --no-build-isolation -e .",
                    "exit_code": 0,
                },
                {"command": discovery.readiness_commands[0], "exit_code": 0},
                {
                    "command": discovery.upstream_test_commands[0],
                    "exit_code": 1,
                    "stderr": "ValueError: Your setup doesn't support bf16/gpu.",
                },
            ],
        },
    )
    evidence = [
        cpu.Citation(
            evidence_id="source/tests/test_cpu.py", quote="args = DPOConfig(output_dir='tmp')"
        )
    ]
    assessment = cpu.CpuFixtureAssessment(
        approved=True,
        essential_gpu_semantics=False,
        explanation="The unchanged fixture checks caching, with no GPU-specific assertion.",
        commands=[
            cpu.CommandAssessment(
                command=discovery.upstream_test_commands[0],
                cpu_semantics_preserved=True,
                explanation="The constructor omits device selection; assertions concern caching.",
                evidence=evidence,
            )
        ],
        evidence=[
            *evidence,
            cpu.Citation(evidence_id="failure", quote="Your setup doesn't support bf16/gpu."),
        ],
    )
    s = SimpleNamespace(
        request=request,
        config=config,
        budget=config.budget("trl-6206"),
        deadline=time.time() + 1200,
        root=tmp_path / "approval",
        assessment=assessment,
        calls=[],
    )

    async def judge(**kwargs):
        s.calls.append(kwargs)
        await kwargs["validate"](s.assessment)
        return s.assessment

    monkeypatch.setattr(cpu.review, "_paged", judge)
    return s


def arguments(s):
    return {key: getattr(s, key) for key in ("config", "budget", "deadline", "root")}


@pytest.mark.asyncio
async def test_review_is_bound_and_does_not_claim_readiness(setup):
    s = setup
    result = await cpu.review_cpu_fixture(s.request, **arguments(s))
    assert result["reference_readiness_passed"] is False
    assert result["requires_full_reference_rerun"] is True
    assert s.calls[0]["budget"] is s.budget
    assert s.calls[0]["deadline"] == s.deadline
    assert s.calls[0]["config"] is s.config
    assert set(s.calls[0]["evidence"]) >= {"failure", "source/tests/test_cpu.py", "documented_api"}
    assert not s.budget.path.exists()  # Mocked judge performs no reservation or effects.
    again = await cpu.review_cpu_fixture(s.request, **arguments(s))
    assert again == result and len(s.calls) == 1


@pytest.mark.asyncio
async def test_completed_rejection_is_not_rerolled(setup):
    s = setup
    s.assessment.approved = False
    s.assessment.essential_gpu_semantics = True
    for _ in range(2):
        with pytest.raises(cpu.CpuFixtureUnsupported, match="rejected"):
            await cpu.review_cpu_fixture(s.request, **arguments(s))
    assert len(s.calls) == 1
    assert json.loads((s.root / "phase.json").read_text())["status"] == "completed"


@pytest.mark.asyncio
async def test_interrupted_review_requires_reconciliation(setup, monkeypatch):
    s = setup

    async def failed(**kwargs):
        raise TimeoutError("original phase stopped")

    monkeypatch.setattr(cpu.review, "_paged", failed)
    with pytest.raises(TimeoutError):
        await cpu.review_cpu_fixture(s.request, **arguments(s))
    with pytest.raises(cpu.CpuFixtureUnsupported, match="reconciliation"):
        await cpu.review_cpu_fixture(s.request, **arguments(s))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["deadline", "recipe", "scope", "source", "evidence", "result", "linked"]
)
async def test_changed_receipt_inputs_fail_without_review(setup, change):
    s = setup
    await cpu.review_cpu_fixture(s.request, **arguments(s))
    if change == "deadline":
        s.deadline += 1
    elif change == "recipe":
        s.request.discovery.dependency_inputs["extra.txt"] = "changed"
    elif change == "scope":
        s.budget.scope = "new-allowance"
    elif change == "source":
        s.request.base_sha = "c" * 40
    elif change == "evidence":
        (s.root / "inputs.json").write_text("{}")
    elif change == "result":
        (s.root / "result.json").write_text("{}")
    else:
        path = s.root / "inputs.json"
        content = path.read_bytes()
        path.unlink()
        other = s.root.parent / "elsewhere.json"
        other.write_bytes(content)
        path.symlink_to(other)
    with pytest.raises(cpu.CpuFixtureUnsupported):
        await cpu.review_cpu_fixture(s.request, **arguments(s))
    assert len(s.calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        "text",
        "missing",
        "unknown",
        "gpu",
        "timeout",
        "order",
        "reset",
        "oversize",
        "ordinary_failure",
    ],
)
def test_ineligible_or_incomplete_input_rejected(setup, change):
    data = setup.request.model_dump(mode="json")
    if change == "text":
        data["source_files"][0]["text"] += "x"
    elif change == "missing":
        data["source_files"] = data["source_files"][1:]
    elif change == "unknown":
        data["constructors"] = ["TrainingArguments.__post_init__"]
    elif change == "gpu":
        data["profile"] = "gpu"
    elif change == "timeout":
        data["readiness"]["checks"][-1]["timed_out"] = True
    elif change == "order":
        data["readiness"]["checks"].pop(1)
    elif change == "reset":
        data["readiness"]["reset"] = None
    elif change == "ordinary_failure":
        data["readiness"]["checks"][-1]["stderr"] = "AssertionError: unexpected cached value"
    else:
        data["readiness"]["checks"][-1]["stderr"] = "x" * cpu.MAX_INPUT_BYTES
    with pytest.raises(ValueError):
        cpu.CpuFixtureRequest.model_validate(data)


@pytest.mark.asyncio
async def test_review_cannot_omit_command_or_fabricate_evidence(setup):
    s = setup
    s.assessment.commands[0].command = "pytest another.py"
    with pytest.raises(ValueError, match="every unchanged command"):
        await cpu.review_cpu_fixture(s.request, **arguments(s))


@pytest.mark.asyncio
async def test_unrelated_config_citation_does_not_support_test_semantics(setup):
    s = setup
    s.assessment.commands[0].evidence = [
        cpu.Citation(evidence_id="source/trl/trainer/base_config.py", quote="class BaseConfig:")
    ]
    with pytest.raises(ValueError, match="actual selected test modules"):
        await cpu.review_cpu_fixture(s.request, **arguments(s))


def test_essential_gpu_semantics_cannot_be_approved(setup):
    data = setup.assessment.model_dump(mode="json")
    data["essential_gpu_semantics"] = True
    with pytest.raises(ValueError, match="Essential GPU"):
        cpu.CpuFixtureAssessment.model_validate(data)


@pytest.fixture
def fake_class():
    @dataclass
    class FakeConfig:
        output_dir: str
        use_cpu: bool = False
        bf16: bool = True
        seed: int = 42

    return FakeConfig


def events():
    return {"calls": {}, "refused": []}


def test_only_omitted_resource_argument_changes_and_lifetime_ends(fake_class):
    cls = fake_class
    original = cls.__init__
    observed = events()
    original_argv = ["tests/test_cpu.py", "-k", "not slow", "-v"]

    def run(argv):
        assert argv == original_argv and argv is not original_argv
        assert cls("out", bf16=False, seed=19).__dict__ == {
            "output_dir": "out",
            "use_cpu": True,
            "bf16": False,
            "seed": 19,
        }
        assert cls("out", True, True, 7).seed == 7
        assert cls("out", use_cpu=True).bf16 is True
        return 1  # Healthy test failure remains a failure, no exception suppression.

    assert cpu._cpu_pytest({"DPOConfig": cls}, original_argv, run, observed) == 1
    assert cls.__init__ is original
    assert cls("out").use_cpu is False
    assert observed["calls"] == {"DPOConfig": {"injected": 1, "explicit_cpu": 2}}


@pytest.mark.parametrize("kwargs", [{"use_cpu": False}, {"use_cpu": None}, {"use_cpu": 0}])
def test_explicit_non_cpu_choice_is_not_overridden(fake_class, kwargs):
    original = fake_class.__init__
    observed = events()
    with pytest.raises(RuntimeError, match="refuses explicit"):
        cpu._cpu_pytest(
            {"DPOConfig": fake_class}, [], lambda _: fake_class("out", **kwargs), observed
        )
    assert fake_class.__init__ is original
    assert observed["refused"] == ["DPOConfig"] and not observed["calls"]


def test_constructor_error_and_pytest_interrupt_restore_methods(fake_class):
    original = fake_class.__init__
    for error in (TypeError, KeyboardInterrupt):

        def fail(_, error=error):
            if error is TypeError:
                fake_class("out", unknown_argument=3)
            raise KeyboardInterrupt

        with pytest.raises(error):
            cpu._cpu_pytest({"DPOConfig": fake_class}, [], fail, events())
        assert fake_class.__init__ is original


def test_unsupported_signature_restores_prior_patch(fake_class):
    original = fake_class.__init__

    class Unsupported:
        def __init__(self, **kwargs):
            pass

    with pytest.raises(RuntimeError, match="signature"):
        cpu._cpu_pytest(
            {"DPOConfig": fake_class, "SFTConfig": Unsupported}, [], lambda _: 0, events()
        )
    assert fake_class.__init__ is original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode, expected",
    [("normal", 0), ("failure", 1), ("explicit", 78), ("source-change", 78), ("wrong-import", 78)],
)
async def test_trusted_runner_with_fake_pytest_and_no_target_imports(
    setup, monkeypatch, fake_class, mode, expected
):
    s = setup
    await cpu.review_cpu_fixture(s.request, **arguments(s))
    runner = cpu.render_cpu_pytest(s.request, 0, **arguments(s))
    repository = s.root.parent / "synthetic-repo"
    for source in s.request.source_files:
        path = repository / source.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source.text)
    before = {p.path: (repository / p.path).read_bytes() for p in s.request.source_files}
    receipt = s.root.parent / "remote-receipt.json"
    monkeypatch.chdir(repository)
    monkeypatch.setattr(sys, "argv", ["trusted-runner.py", str(receipt)])
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setattr(importlib.metadata, "version", lambda _: "4.56.2")
    monkeypatch.setattr(importlib, "import_module", lambda _: SimpleNamespace(DPOConfig=fake_class))
    monkeypatch.setattr(
        inspect,
        "getfile",
        lambda _: str(
            repository / ("elsewhere.py" if mode == "wrong-import" else "trl/trainer/dpo_config.py")
        ),
    )
    commands = []

    def git(argv, **kwargs):
        commands.append(argv)
        assert argv[0] == "git" and kwargs["timeout"] <= 10
        return SimpleNamespace(stdout=s.request.head_sha + "\n")

    monkeypatch.setattr(subprocess, "run", git)

    def pytest_main(argv):
        assert argv == ["tests/test_cpu.py", "-k", "not slow", "-v"]
        assert fake_class("out").use_cpu is True
        if mode == "explicit":
            # A test catching the error cannot disguise unsupported adaptation.
            with pytest.raises(RuntimeError):
                fake_class("out", use_cpu=False)
        if mode == "source-change":
            (repository / "tests/test_cpu.py").write_text("changed by synthetic test")
        return 1 if mode == "failure" else 0

    monkeypatch.setitem(sys.modules, "pytest", SimpleNamespace(main=pytest_main))
    original = fake_class.__init__
    with pytest.raises(SystemExit) as stop:
        exec(compile(runner, "trusted-host-runner", "exec"), {})
    assert stop.value.code == expected
    assert fake_class.__init__ is original
    result = json.loads(receipt.read_text())
    assert result["exit_code"] == expected
    assert result["source_unchanged"] is (mode != "source-change")
    assert len(commands) == 4
    if mode != "source-change":
        assert before == {
            p.path: (repository / p.path).read_bytes() for p in s.request.source_files
        }


def test_no_runner_without_approval(setup):
    with pytest.raises(FileNotFoundError):
        cpu.render_cpu_pytest(setup.request, 0, **arguments(setup))


@pytest.mark.asyncio
async def test_combined_trusted_observer_executes_fixed_context_and_coverage_hooks(
    setup, monkeypatch, fake_class
):
    from repo2rlenv.tasksmith import build

    s = setup
    await cpu.review_cpu_fixture(s.request, **arguments(s))
    repository = s.root.parent / "synthetic-observer-repo"
    for source in s.request.source_files:
        path = repository / source.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source.text)
    monkeypatch.chdir(repository)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setattr(importlib.metadata, "version", lambda _: "4.56.2")
    monkeypatch.setattr(importlib, "import_module", lambda _: SimpleNamespace(DPOConfig=fake_class))
    original_getfile = inspect.getfile
    monkeypatch.setattr(
        inspect,
        "getfile",
        lambda value: (
            str(repository / "trl/trainer/dpo_config.py")
            if value is fake_class
            else original_getfile(value)
        ),
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=s.request.head_sha + "\n")
    )
    node = "tests/test_cpu.py::test_cpu[seed=42]"

    def pytest_main(argv, plugins):
        assert argv == ["tests/test_cpu.py", "-k", "not slow", "-v"]
        assert fake_class("out").use_cpu is True
        observer = plugins[0]
        observer.pytest_collection_finish(SimpleNamespace(items=[SimpleNamespace(nodeid=node)]))
        observer.pytest_runtest_logreport(SimpleNamespace(when="call", skipped=False, nodeid=node))
        return 0

    monkeypatch.setitem(sys.modules, "pytest", SimpleNamespace(main=pytest_main))
    transfers = {}

    async def shell(command, timeout_sec):
        assert "python -I -c" in command
        script, payload = shlex.split(command)[-2:]
        capture = json.loads(payload)
        for key in ("data_path", "receipt_path"):
            remote_path = capture[key]
            local_path = s.root.parent / ("synthetic-" + key + ".json")
            transfers[remote_path] = local_path
            capture[key] = str(local_path)
        monkeypatch.setattr(sys, "argv", ["trusted-observer", json.dumps(capture)])
        # Execute only the trusted host observer with fake pytest/config imports.
        with pytest.raises(SystemExit) as stopped:
            exec(compile(script, "trusted-observer", "exec"), {})
        return json.dumps({"exit_code": stopped.value.code, "stdout": "", "stderr": ""})

    async def read(path):
        return transfers[path].read_bytes()

    original = fake_class.__init__
    report = await build._pytest_observation(
        shell,
        {"head_sha": s.request.head_sha},
        s.request.discovery.upstream_test_commands[0],
        collect_only=False,
        read=read,
        cpu_context={"request": s.request, **arguments(s)},
    )
    assert report["collected"] == report["executed"] == [node]
    assert report["cpu_fixture"]["source_unchanged"] is True
    assert report["cpu_fixture"]["calls"]["DPOConfig"]["injected"] == 1
    assert fake_class.__init__ is original


@pytest.mark.asyncio
async def test_expired_original_deadline_cannot_launch_or_review(setup, monkeypatch):
    s = setup
    await cpu.review_cpu_fixture(s.request, **arguments(s))
    monkeypatch.setattr(cpu.time, "time", lambda: s.deadline + 1)
    with pytest.raises(TimeoutError, match="Original"):
        cpu.render_cpu_pytest(s.request, 0, **arguments(s))
    s.root = s.root.parent / "expired-unclaimed"
    with pytest.raises(TimeoutError, match="Original"):
        await cpu.review_cpu_fixture(s.request, **arguments(s))
    assert not s.root.exists()
