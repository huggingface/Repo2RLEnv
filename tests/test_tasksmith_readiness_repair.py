from __future__ import annotations

import hashlib
import json
import shlex
import time
from copy import deepcopy
from itertools import pairwise
from types import SimpleNamespace

import pytest

from repo2rlenv.tasksmith import readiness_repair as repair
from repo2rlenv.tasksmith import worker
from repo2rlenv.tasksmith.authoring import Discovery
from repo2rlenv.tasksmith.config import TasksmithConfig


@pytest.fixture
def setup(tmp_path, monkeypatch):
    config = TasksmithConfig(
        ledger_path=tmp_path / "budget.json", ledger_limit_usd=50, campaign_id="smoke-fixture"
    )
    source = {
        "id": "library-1",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "body": "Fingerprint the preprocessing callable using the library serializer.",
    }
    old = 'python -c "import pickle; f = lambda x: x + 3; pickle.dumps(f)"'
    replacement = "python -c " + shlex.quote(
        "import dill; from datasets.utils._dill import dumps; "
        "f = lambda x: x + 3; restored = dill.loads(dumps(f)); assert restored(4) == 7"
    )
    discovery = Discovery(
        useful_outcome="Fingerprint preprocessing callables without capturing unrelated state.",
        behaviors=[
            {
                "id": "stable_cache",
                "outcome": "Unchanged callable inputs retain the same cache key.",
                "source_evidence": ["library/prepare.py"],
            }
        ],
        dependency_dockerfile="FROM python:3.12-slim@sha256:" + "c" * 64,
        dependency_inputs={"requirements.txt": "datasets==4.7.0; dill==0.4.0"},
        readiness_commands=["python -c 'import library'", old, "python -c 'import pytest'"],
        upstream_test_commands=["python -m pytest tests/test_cache.py -q"],
        upstream_tests="relevant_tests",
        resource_rationale="Small independent fixtures use the real installed dependency APIs.",
    )
    readiness = {
        "passed": False,
        "checks": [
            {
                "command": f"git checkout --detach {source['head_sha']} && python -m pip install --no-index --no-deps --no-build-isolation -e .",
                "exit_code": 0,
            },
            {"command": discovery.readiness_commands[0], "exit_code": 0},
            {
                "command": old,
                "exit_code": 1,
                "stderr": "PicklingError: attribute lookup <lambda> on __main__ failed",
                "timed_out": False,
            },
        ],
        "reset": {"exit_code": 0},
    }
    observations = {
        "serializer": "Captured installed source: from .utils._dill import dumps; Hasher.hash calls dumps(value). Captured dill.dumps(lambda) returned bytes."
    }
    evidence = [
        {"evidence_id": "observation/serializer", "quote": "from .utils._dill import dumps"}
    ]
    proposal = {
        "classification": "invalid_generated_smoke",
        "failed_index": 1,
        "original_command": old,
        "replacement_command": replacement,
        "diagnosis": "The smoke selected stdlib pickle instead of the actual library serializer.",
        "preserved_capability": "Serialize a callable through the installed datasets serializer.",
        "expected_observation": "The restored x + 3 callable returns 7 for input 4.",
        "evidence": evidence,
    }
    assessment = {
        "classification": "invalid_generated_smoke",
        "approved": True,
        "explanation": "The captured serializer source and failure identify an invalid smoke API, while the replacement observes the original callable capability.",
        "preserved_capability": proposal["preserved_capability"],
        "expected_observation": proposal["expected_observation"],
        "evidence": [
            {"evidence_id": "failure", "quote": "attribute lookup <lambda> on __main__ failed"},
            *evidence,
        ],
    }
    s = SimpleNamespace(
        config=config,
        source=source,
        discovery=discovery,
        readiness=readiness,
        observations=observations,
        proposal=proposal,
        assessment=assessment,
        root=tmp_path / "bootstrap-smoke-repair-1",
        budget=config.budget(source["id"]),
        deadline=time.time() + 60,
        calls=[],
        shell_calls=[],
        read=True,
        diagnostics=False,
        diagnostic_requests=1,
        diagnostic_origin="dependency",
        diagnostic_path="datasets/fingerprint.py",
        source_text="Captured serializer Python source",
        diagnostic_pages=False,
        source_stdout=[],
        interrupt=False,
        author_charge=0.0,
    )

    async def shell(command, timeout_sec):
        s.shell_calls.append((command, timeout_sec))
        words = shlex.split(command)
        assert words[:3] == ["python", "-I", "-c"]
        assert words[3] == repair._SOURCE_READER
        request = json.loads(words[4])
        assert request["path"] == s.diagnostic_path and request["origin"] == s.diagnostic_origin
        if request["origin"] in {"base", "head"}:
            assert request["sha"] == source[request["origin"] + "_sha"]
        start = request["offset"]
        end = min(start + request["length"], len(s.source_text))
        stdout = (
            json.dumps(
                {
                    "origin": request["origin"],
                    "path": request["path"],
                    "sha256": hashlib.sha256(s.source_text.encode()).hexdigest(),
                    "offset": start,
                    "next_offset": end,
                    "total_characters": len(s.source_text),
                    "text": s.source_text[start:end],
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        s.source_stdout.append(stdout)
        assert len(stdout.encode()) <= 20_000
        delivered = stdout.encode()[-20_000:].decode()
        return json.dumps({"exit_code": 0, "stdout": delivered, "stderr": ""})

    s.shell = shell

    async def run(**kwargs):
        s.calls.append(kwargs)
        assert kwargs["budget"] is s.budget
        operation = json.loads((kwargs["trace"].parent / "operation.json").read_text())
        assert operation["deadline"] == s.deadline
        if kwargs["model"] == config.author_model:
            assert kwargs["runtime"] == config.author_runtime
            schema = kwargs["tools"][0]["function"]["parameters"]
            assert "dependency_dockerfile" not in schema["properties"]
            assert "readiness_commands" not in schema["properties"]
            assert "shell" not in kwargs["handlers"]
            if s.diagnostics:
                offset = 0
                for i in range(s.diagnostic_requests):
                    result = await kwargs["handlers"]["read_source"](
                        path=s.diagnostic_path, origin=s.diagnostic_origin, offset=offset
                    )
                    if i < repair.MAX_DIAGNOSTICS:
                        assert json.loads(result)["evidence_id"] == f"diagnostic/{i + 1}"
                        assert len(result) <= 24_000
                        if s.diagnostic_pages:
                            offset = json.loads(result)["captured"]["next_offset"]
                    else:
                        assert "allowance exhausted" in result
            if s.author_charge:
                key = s.budget.reserve(s.author_charge, "mock-author")
                s.budget.settle(key, s.author_charge)
            value = s.proposal
        else:
            assert kwargs["model"] == config.reviewer_model and kwargs["runtime"] == "langgraph"
            assert set(kwargs["handlers"]) == {
                "submit_artifact",
                "revise_artifact",
                "read_evidence",
            }
            if s.interrupt:
                raise RuntimeError("Mock reviewer interrupted")
            if s.read:
                for key, length in json.loads(kwargs["prompt"])["required_evidence"].items():
                    offset = 0
                    while offset < length:
                        reply = json.loads(
                            await kwargs["handlers"]["read_evidence"](
                                requests=[{"evidence_id": key, "offset": offset, "length": 12000}]
                            )
                        )
                        offset = reply["pages"][0]["next_offset"]
            value = s.assessment
        s.last_reply = await kwargs["handlers"]["submit_artifact"](**value)

    monkeypatch.setattr(worker, "run_agent", run)
    return s


async def invoke(s, **overrides):
    args = dict(
        config=s.config,
        source=s.source,
        discovered=s.discovery,
        readiness=s.readiness,
        root=s.root,
        budget=s.budget,
        deadline=s.deadline,
        observations=s.observations,
        shell=s.shell if s.diagnostics else None,
    )
    args.update(overrides)
    return await repair.correct_generated_smoke(**args)


@pytest.mark.asyncio
async def test_only_failed_check_changes_after_independent_full_evidence_review(setup):
    s = setup
    before = deepcopy(s.discovery.model_dump())
    original_readiness = deepcopy(s.readiness)
    corrected = await invoke(s)
    expected = deepcopy(before)
    expected["readiness_commands"][1] = s.proposal["replacement_command"]
    assert corrected.model_dump() == expected
    assert s.discovery.model_dump() == before and s.readiness == original_readiness
    assert len(s.calls) == 2 and not s.shell_calls and s.budget.spent == 0
    result = json.loads((s.root / "result.json").read_text())
    assert not result["reference_readiness_passed"] and result["requires_full_reference_rerun"]
    assert result["evidence_files"]["assessment/read-coverage.json"]
    saved = {p: p.read_bytes() for p in s.root.rglob("*") if p.is_file()}
    assert await invoke(s) == corrected
    assert len(s.calls) == 2 and saved == {p: p.read_bytes() for p in saved}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "classification",
    ["dependency_failure", "reference_failure", "unknown", "invalid_generated_smoke"],
)
async def test_rejected_review_is_durable_and_only_confirmed_dependency_allows_fallback(
    setup, classification
):
    s = setup
    s.assessment.update(classification=classification, approved=False)
    error = (
        repair.SmokeDependencyFailure
        if classification == "dependency_failure"
        else repair.SmokeRepairError
    )
    for _ in range(2):
        with pytest.raises(error):
            await invoke(s)
    assert len(s.calls) == 2
    assert json.loads((s.root / "phase.json").read_text())["status"] == "completed"
    assert json.loads((s.root / "result.json").read_text())["discovery"] == s.discovery.model_dump()


@pytest.mark.asyncio
async def test_author_cannot_reclassify_missing_dependency_without_independent_approval(setup):
    s = setup
    s.proposal.update(classification="dependency_failure", replacement_command=None)
    with pytest.raises(ValueError, match="without a validated artifact"):
        await invoke(s)
    assert "No proposed command correction" in s.last_reply
    assert not (s.root / "result.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    ["failed_index", "original_command", "replacement_command", "evidence", "frozen_field"],
)
async def test_invalid_proposal_cannot_advance_to_review(setup, changed):
    s = setup
    if changed == "failed_index":
        s.proposal[changed] = 0
    elif changed == "original_command":
        s.proposal[changed] = "different failure"
    elif changed == "replacement_command":
        s.proposal[changed] = "true"
    elif changed == "frozen_field":
        s.proposal["dependency_dockerfile"] = "change unrelated frozen field"
    else:
        s.proposal[changed][0]["quote"] = "Invented serializer source"
    with pytest.raises(ValueError, match="without a validated artifact"):
        await invoke(s)
    assert len(s.calls) == 1 and not (s.root / "result.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unread", "fake_quote", "author_only", "interrupted"])
async def test_incomplete_or_unsupported_assessment_never_approves_or_rerolls(setup, mode):
    s = setup
    if mode == "unread":
        s.read = False
    elif mode == "fake_quote":
        s.assessment["evidence"][0]["quote"] = "Invented failure observation"
    elif mode == "author_only":
        s.assessment["evidence"] = [
            s.assessment["evidence"][0],
            {"evidence_id": "proposal", "quote": s.proposal["diagnosis"]},
        ]
    else:
        s.interrupt = True
    with pytest.raises((ValueError, RuntimeError)):
        await invoke(s)
    assert not (s.root / "result.json").exists()
    with pytest.raises(repair.SmokeRepairError, match="reconciliation"):
        await invoke(s)
    assert len(s.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["dependency", "base", "head"])
async def test_diagnostics_inspect_only_host_authored_pinned_source_and_reviewer_reads_it(
    setup, origin
):
    s = setup
    s.diagnostics, s.diagnostic_origin = True, origin
    await invoke(s)
    assert len(s.shell_calls) == 1 and 0 < s.shell_calls[0][1] <= 20
    evidence = json.loads((s.root / "evidence.json").read_text())
    assert "diagnostic/1" in evidence
    coverage = json.loads((s.root / "assessment/read-coverage.json").read_text())
    assert coverage["spans"]["diagnostic/1"] == [[0, len(evidence["diagnostic/1"])]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/private/secret.py", "../secret.py", "datasets/../secret.py", "config.json"]
)
async def test_invalid_diagnostic_path_never_reaches_remote_shell(setup, path):
    s = setup
    s.diagnostics, s.diagnostic_path = True, path
    with pytest.raises(ValueError):
        await invoke(s)
    assert not s.shell_calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["source", "checks", "recipe", "upstream", "deadline", "budget", "observations", "config"],
)
async def test_retained_identity_rejects_changes_before_calls(setup, change):
    s = setup
    await invoke(s)
    if change == "source":
        s.source["base_sha"] = "d" * 40
    elif change == "checks":
        s.discovery.readiness_commands[-1] = "python -c 'import other'"
    elif change == "recipe":
        s.discovery.dependency_dockerfile += "\n"
    elif change == "upstream":
        s.discovery.upstream_test_commands = ["pytest unrelated.py"]
    elif change == "deadline":
        s.deadline += 1
    elif change == "budget":
        s.budget.scope = "new-scope"
    elif change == "observations":
        s.observations["serializer"] += " Changed evidence."
    else:
        s.config.author_turns += 1
    with pytest.raises(repair.SmokeRepairError, match="different inputs"):
        await invoke(s)
    assert len(s.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["result.json", "evidence.json", "assessment/read-coverage.json"])
async def test_cached_evidence_tampering_is_rejected_before_calls(setup, path):
    s = setup
    await invoke(s)
    p = s.root / path
    p.write_text(p.read_text() + "\n")
    with pytest.raises(repair.SmokeRepairError, match="changed"):
        await invoke(s)
    assert len(s.calls) == 2


@pytest.mark.asyncio
async def test_phase_keeps_original_budget_and_remaining_allowance(setup):
    s = setup
    s.author_charge = 4.25
    await invoke(s)
    assert s.calls[0]["max_cost"] == 6
    assert s.calls[1]["max_cost"] == pytest.approx(1.75)
    assert s.budget.spent == pytest.approx(4.25)


@pytest.mark.asyncio
async def test_diagnostic_call_limit_does_not_run_extra_remote_effects(setup):
    s = setup
    s.diagnostics, s.diagnostic_requests = True, repair.MAX_DIAGNOSTICS + 1
    await invoke(s)
    assert len(s.shell_calls) == repair.MAX_DIAGNOSTICS


@pytest.mark.asyncio
async def test_escaped_unicode_source_pages_survive_shell_tail_and_tool_delivery(setup):
    s = setup
    s.source_text = '"\\\n\t\x00😀α' * 700
    s.diagnostics = s.diagnostic_pages = True
    s.diagnostic_requests = (
        len(s.source_text) + repair.MAX_DIAGNOSTIC_CHARACTERS - 1
    ) // repair.MAX_DIAGNOSTIC_CHARACTERS
    await invoke(s)
    evidence = json.loads((s.root / "evidence.json").read_text())
    pages = [json.loads(value) for key, value in evidence.items() if key.startswith("diagnostic/")]
    assert "".join(page["text"] for page in pages) == s.source_text
    assert pages[0]["offset"] == 0 and pages[-1]["next_offset"] == len(s.source_text)
    assert all(len(page["text"]) <= 1500 for page in pages)
    assert all(len(stdout.encode()) <= 20_000 for stdout in s.source_stdout)
    for previous, current in pairwise(pages):
        assert previous["next_offset"] == current["offset"]
        assert previous["sha256"] == current["sha256"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        "truncated",
        "nonzero",
        "wrong_range",
        "wrong_path",
        "bad_digest",
        "extra_field",
        "boolean_offset",
        "wrong_origin",
        "invalid_outer",
        "false_total",
        "wrong_text",
    ],
)
async def test_invalid_nested_source_observation_never_becomes_citeable(setup, invalid):
    s = setup
    s.diagnostics = True

    async def corrupted(command, timeout_sec):
        observed = json.loads(await s.shell(command, timeout_sec))
        source = json.loads(observed["stdout"])
        if invalid == "wrong_range":
            source["next_offset"] -= 1
        elif invalid == "wrong_path":
            source["path"] = "another.py"
        elif invalid == "bad_digest":
            source["sha256"] = "not-a-sha256"
        elif invalid == "extra_field":
            source["invented"] = True
        elif invalid == "boolean_offset":
            source["offset"] = False
        elif invalid == "wrong_origin":
            source["origin"] = "head"
        elif invalid == "false_total":
            source["total_characters"] = 1
        elif invalid == "wrong_text":
            source["text"] += "invented"
        observed["stdout"] = json.dumps(source)
        if invalid == "truncated":
            observed["stdout"] = observed["stdout"][20:]
        elif invalid == "nonzero":
            observed["exit_code"] = 1
        elif invalid == "invalid_outer":
            return "tail of broken outer JSON"
        return json.dumps(observed)

    with pytest.raises(ValueError, match="Invalid source observation"):
        await invoke(s, shell=corrupted)
    assert len(s.calls) == 1 and len(s.shell_calls) == 1
    assert not (s.root / "evidence.json").exists()
    assert not (s.root / "assessment").exists()
    retained = json.loads((s.root / "diagnostics.json").read_text())
    assert retained[0]["validation_error"] and "raw_response" in retained[0]


@pytest.mark.asyncio
async def test_expired_deadline_or_oversized_evidence_never_starts_worker(setup):
    s = setup
    with pytest.raises(TimeoutError, match="deadline exhausted"):
        await invoke(s, deadline=time.time() - 1)
    with pytest.raises(repair.SmokeRepairError, match="evidence exceeds"):
        await invoke(s, observations={"huge": "x" * repair.MAX_EVIDENCE_CHARACTERS})
    assert not s.calls and not s.shell_calls


@pytest.mark.asyncio
async def test_spent_phase_does_not_start_reviewer_with_reset_allowance(setup):
    s = setup
    s.author_charge = s.config.author_stage_limit_usd
    with pytest.raises(repair.BudgetExceeded, match="allowance exhausted"):
        await invoke(s)
    assert len(s.calls) == 1 and s.budget.spent == s.author_charge
    with pytest.raises(repair.SmokeRepairError, match="reconciliation"):
        await invoke(s)


@pytest.mark.parametrize(
    "invalid", [None, [], {}, {"passed": False, "checks": [None]}, {"passed": False, "checks": [1]}]
)
def test_malformed_failure_receipt_is_ineligible(setup, invalid):
    assert repair.generated_smoke_failure(setup.discovery, invalid) is None


@pytest.mark.parametrize(
    "code",
    [
        "pass",
        "import dill",
        "assert True",
        "x = 1; assert 1 == 1",
        "import dill\ntry:\n x = dill.dumps(lambda x:x)\nexcept Exception:\n pass\nassert x",
        "import pickle; f = lambda x:x; f.__name__ = 'f'; assert pickle.dumps(f)",
        "import pickle; f = lambda x:x; setattr(f, '__qualname__', 'f'); assert pickle.dumps(f)",
        "import pickle; pickle.dumps = lambda x:b'pass'; x = pickle.dumps(1); assert x",
        "x = 1; exec('pass'); assert x",
        "broken : python syntax",
    ],
)
def test_obvious_weakenings_rejected_without_execution(code):
    with pytest.raises(ValueError):
        repair._replacement("python -c " + shlex.quote(code))


@pytest.mark.parametrize(
    "change", ["passed", "install", "upstream", "timeout", "signal", "reset", "no_output", "order"]
)
def test_only_observed_generated_failure_is_eligible(setup, change):
    s = setup
    assert repair.generated_smoke_failure(s.discovery, s.readiness) == 1
    r = s.readiness
    if change == "passed":
        r["passed"] = True
    elif change == "install":
        r["checks"] = [{"command": "install", "exit_code": 1, "stderr": "build failed"}]
    elif change == "upstream":
        r["checks"][-1]["command"] = s.discovery.upstream_test_commands[0]
    elif change == "timeout":
        r["checks"][-1]["timed_out"] = True
    elif change == "signal":
        r["checks"][-1]["exit_code"] = -9
    elif change == "reset":
        r["reset"]["exit_code"] = 1
    elif change == "no_output":
        r["checks"][-1]["stderr"] = ""
    else:
        r["checks"][1]["command"] = "unrelated passed command"
    assert repair.generated_smoke_failure(s.discovery, r) is None
