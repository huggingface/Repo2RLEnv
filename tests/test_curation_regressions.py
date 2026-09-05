from __future__ import annotations

import ast
import builtins
import hashlib
import json
import tomllib
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "curation_regressions"


@pytest.fixture(autouse=True)
def forbid_target_imports(monkeypatch):
    original = builtins.__import__
    forbidden = {"torch", "peft", "trl", "transformers", "diffusers"}

    def checked(name, *args, **kwargs):
        if name.split(".", 1)[0] in forbidden:
            raise AssertionError(f"Local target import is forbidden in this corpus: {name}")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", checked)


def _case(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _function(case, key, namespace):
    """Load a retained pure definition, never a worker import or whole task module."""
    snippet = case["snippets"][key]
    text = snippet["text"]
    assert hashlib.sha256(text.encode()).hexdigest() == snippet["text_sha256"]
    tree = ast.parse(text)
    assert len(tree.body) == 1 and isinstance(tree.body[0], ast.FunctionDef)
    assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(tree))
    assert not tree.body[0].decorator_list
    exec(compile(tree, f"{case['id']}:{key}", "exec"), namespace)
    return namespace[tree.body[0].name]


def test_wrong_math_passes_recorded_cache_consistency_but_fails_independent_norm():
    np = pytest.importorskip("numpy")
    case = _case("wrong_dora_norm")
    witness = case["witness"]
    weight = np.asarray(witness["weight"]) + np.asarray(witness["delta"])
    inputs, magnitude = np.asarray(witness["input"]), np.asarray(witness["magnitude"])
    expected = (inputs @ weight.T) * (magnitude / np.linalg.norm(weight, axis=1))
    wrong = (inputs @ weight.T) * (magnitude / witness["bad_norm"])
    np.testing.assert_allclose(expected, [[3.6, 0], [4.8, 3]])
    assert not np.allclose(expected, inputs @ weight.T)
    namespace = {"np": np}
    for name in case["snippets"]:
        _function(case, name, namespace)
    for output in (expected, wrong):
        response = {
            "num_caches": 1,
            "base_vs_dora_differ": not np.allclose(output, inputs @ weight.T),
            "caches_empty_before_any_caching": [False],
            "caches_nonempty_after_first_cached_call": [True],
            "caches_nonempty_after_exit_forward": [False],
            "caches_nonempty_after_train_switch": [False],
            "caches_nonempty_during_train_forward": [False],
            **{
                key: output.tolist()
                for key in (
                    "dora_result",
                    "cached_result_1",
                    "cached_result_2",
                    "after_exit_result",
                    "train_mode_result",
                    "perm_cached_result",
                )
            },
        }
        observed = {"linear": response}
        namespace["test_dora_caching_linear_matches_uncached_result"](observed)
        for name in case["snippets"]:
            if name.startswith("test_") and "linear_matches" not in name:
                namespace[name](observed, "linear")
    with pytest.raises(AssertionError):
        np.testing.assert_allclose(wrong, expected, atol=1e-6, rtol=1e-6)


@pytest.mark.parametrize(
    "layout", ["rows_first", "weight_first", "split_reordered", "wrong_weight"]
)
def test_projection_observer_accepts_equivalent_orientation_without_losing_weight_check(layout):
    np = pytest.importorskip("numpy")
    case = _case("projection_orientation")
    scope = {"np": np}
    for name in case["snippets"]:
        if name.startswith("_"):
            _function(case, name, scope)

    def observe(payload):
        vectors = list(payload["block_params"])
        if payload["block_share"]:
            vectors *= payload["in_features"] // payload["block_size"]
        cayley = (
            scope["_neumann_cayley"] if payload["use_cayley_neumann"] else scope["_exact_cayley"]
        )
        args = (payload["num_cayley_neumann_terms"],) if payload["use_cayley_neumann"] else ()
        rotation = scope["_block_diag"]([cayley(v, payload["block_size"], *args) for v in vectors])
        rows = np.asarray(payload["x"]) @ rotation
        weight = np.asarray(payload["W"])
        np.testing.assert_allclose((weight @ rows.T).T, rows @ weight.T, atol=1e-12)
        if layout == "weight_first":
            operands = [(weight, rows.T)]
        elif layout == "split_reordered":
            operands = [(rows[2:][::-1], weight.T), (rows[:2], weight.T)]
        else:
            operands = [(rows, weight.T + (1 if layout == "wrong_weight" else 0))]
        return {
            "forward": (rows @ weight.T + payload["b"]).tolist(),
            "linear_ops": [{"left": a.tolist(), "right": b.tolist()} for a, b in operands],
            "base_weight_after_forward": payload["W"],
            "base_bias_after_forward": payload["b"],
            "trainable_names": ["base_model.model.proj.oft_R.default.weight"],
        }

    scope["_run"] = observe
    for version in ("before", "after"):
        check = _function(case, version, scope)
        if layout == "wrong_weight" or (version == "before" and layout == "weight_first"):
            with pytest.raises(AssertionError, match="unchanged base weight"):
                check()
        else:
            check()


def test_package_helper_collection_follows_actual_finalized_artifact_scope(tmp_path, monkeypatch):
    pytest.importorskip("harbor")
    # A pinned recipe makes finalization read/write-only, even without network access.
    import urllib.request

    from repo2rlenv.curation.artifacts import finalize
    from repo2rlenv.curation.models import Contract

    def no_network(*args, **kwargs):
        raise AssertionError("regression corpus must never use the network")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    case = _case("package_artifacts")
    source = {**case["source"], "id": "huggingface-peft-2575"}
    assert "@sha256:" in case["pinned_recipe"]
    before = Contract.model_validate(case["before_contract"])
    after = before.model_copy(update={"source_paths": case["after_source_paths"]})
    submission = tmp_path / "submission"
    for name in ("config.py", "layer.py", "model.py", "__init__.py", "rotation.py"):
        path = submission / "src/peft/tuners/oft" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# inert artifact fixture\n")
    for version, contract in (("before", before), ("after", after)):
        task = tmp_path / version
        required_tests = sorted({test for req in contract.requirements for test in req.tests})
        tests = "from probe import run_probe\n" + "\n".join(
            f"def {name}():\n    assert run_probe('print(1)', {{}}) == 1\n"
            for name in required_tests
        )
        files = {
            "contract.json": contract.model_dump_json(),
            "instruction.md": "Implement input rotation in src/peft/tuners/oft/; sibling helpers are allowed.",
            "environment/Dockerfile": case["pinned_recipe"],
            "solution/solve.sh": "#!/bin/sh\ntrue\n",
            "tests/test_contract.py": tests,
        }
        for name, text in files.items():
            path = task / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        finalize(task, source)
        config = tomllib.loads((task / "task.toml").read_text())
        included = set()
        for artifact in config["artifacts"]:
            path = submission / Path(artifact["source"]).relative_to("/workspace")
            included.update(
                p.relative_to(submission).as_posix()
                for p in ([path] if path.is_file() else path.rglob("*"))
                if p.is_file()
            )
        assert (case["helper_path"] in included) is (version == "after")
        assert ("src/peft/tuners/oft/__init__.py" in included) is (version == "after")
        assert "src/peft/tuners/oft/layer.py" in included
        assert "src/peft/tuners/lora/layer.py" not in included
        if version == "after":
            assert (
                "RUN rm -rf /workspace/src/peft/tuners/oft\n"
                in (task / "tests/Dockerfile").read_text()
            )


def _teacher_observation(case, *, enforce_labels_shape):
    """Independent list semantics for the retained shape/prompt branch witness."""
    ids, attention = case["input_ids"], case["attention_mask"]
    labels, prompt = case.get("labels"), case.get("prompt_attention_mask")

    def shape(value):
        return len(value), tuple(map(len, value))

    try:
        if shape(ids) != shape(attention):
            raise ValueError("input/attention shapes")
        if labels is not None and enforce_labels_shape and shape(labels) != shape(ids):
            raise ValueError("labels shape")
        if prompt is not None:
            lengths = list(map(sum, prompt))
        else:
            if labels is None or shape(labels) != shape(ids):
                raise ValueError("missing or wrong-shaped labels")
            lengths = [
                sum(mask) - sum(token != -100 for token in row)
                for mask, row in zip(attention, labels, strict=True)
            ]
        trimmed = [
            [token for token, real in zip(row, mask, strict=True) if real]
            for row, mask in zip(ids, attention, strict=True)
        ]
        if any(n < 0 or n > len(row) for n, row in zip(lengths, trimmed, strict=True)):
            raise ValueError("prompt length")
        return {
            "ok": True,
            "trimmed": trimmed,
            "plens": lengths,
            "clens": [len(row) - n for row, n in zip(trimmed, lengths, strict=True)],
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


def test_prompt_metadata_cannot_hide_labels_shape_error_in_reference():
    case = _case("labels_shape_reference")
    assert _teacher_observation(case["witness"], enforce_labels_shape=False)["ok"]
    assert not _teacher_observation(case["witness"], enforce_labels_shape=True)["ok"]
    for fixed in (False, True):

        def observe(code, payload, enforce=fixed):
            return {
                "teacher_inputs": [
                    _teacher_observation(item, enforce_labels_shape=enforce)
                    for item in payload["teacher_inputs"]
                ]
            }

        scope = {"run_probe": observe, "PROBE_IMPORT_AND_RUN": "unused worker", "json": json}
        _function(case, "valid", scope)()
        _function(case, "before", scope)()
        corrected = _function(case, "after", scope)
        if fixed:
            corrected()
        else:
            with pytest.raises(AssertionError):
                corrected()


def test_optional_review_finding_has_a_real_inheritance_counterexample():
    case = _case("optional_required_support")

    class BaseTunerLayer:
        def supports_lora_conversion(self):
            return False

    class LoraLayer(BaseTunerLayer):
        pass

    class Linear(LoraLayer):
        def supports_lora_conversion(self):
            return True

    class Embedding(LoraLayer):
        pass

    class Conv2d(LoraLayer):
        pass

    class SelectedOnly(BaseTunerLayer):
        def supports_lora_conversion(self, name="default"):
            return name == "chosen"

    def aggregate(layers, name="default"):
        return bool(layers) and all(
            layer.supports_lora_conversion(name)
            if isinstance(layer, SelectedOnly)
            else layer.supports_lora_conversion()
            for layer in layers
        )

    for shared_true in (False, True):
        LoraLayer.supports_lora_conversion = lambda self, value=shared_true: value
        chosen = SelectedOnly()
        hooks = {
            "default_false": BaseTunerLayer().supports_lora_conversion(),
            "selected_true": aggregate([chosen], "chosen"),
            "other_false": aggregate([chosen], "other"),
            "wrapper_selected_true": aggregate([chosen], "chosen"),
            "wrapper_other_false": aggregate([chosen], "other"),
            "empty_false": aggregate([]),
            "empty_wrapper_false": aggregate([]),
            "mixed_false": aggregate([chosen, BaseTunerLayer()], "chosen"),
            "mixed_error": {"type": "TypeError", "message": "Unsupported layer"},
            "empty_error": {"type": "TypeError"},
        }
        scope = {}
        _function(case, "before", scope)(({"hooks": hooks}, {}))
        assert Linear().supports_lora_conversion()
        observations = []
        for kind, cls in (("embedding", Embedding), ("conv2d", Conv2d)):
            support = cls().supports_lora_conversion()
            observations.append(
                {
                    "kind": kind,
                    "layer_support": support,
                    "tuner_support": aggregate([cls()]),
                    "wrapper_support": aggregate([cls()]),
                    "conversion_error": None if support else "TypeError",
                }
            )
        scope.update(
            run_probe=lambda *args, result=observations, **kwargs: result,
            UNSUPPORTED_MODEL_PROBE_CODE="unused worker",
        )
        corrected = _function(case, "after", scope)
        if shared_true:
            with pytest.raises(AssertionError):
                corrected()
        else:
            corrected()


def test_context_storage_census_catches_hook_bypass_without_counting_aliases_twice():
    case = _case("backward_context_storage")
    live = weakref.WeakSet()

    class Storage:
        def __init__(self, pointer, size):
            self.pointer, self.size = pointer, size

        def data_ptr(self):
            return self.pointer

        def nbytes(self):
            return self.size

    class Tensor:
        layout = "strided"

        def __init__(self, storage):
            self.storage = storage
            live.add(self)

        def untyped_storage(self):
            return self.storage

    saved = {}
    base = Tensor(Storage(1, 512 * 5 * 4))
    scope = {
        "original_storage": {1},
        "saved_storage": saved,
        "gc": SimpleNamespace(get_objects=lambda: list(live)),
        "torch": SimpleNamespace(Tensor=Tensor, strided="strided"),
    }
    pack = _function(case, "before_pack", scope)
    census = _function(case, "after_census", scope)
    baseline = set(census())
    fixture = case["witness"]
    budget = (
        8 * fixture["chunk_size"] * fixture["vocab"] * fixture["element_bytes"]
        + 8 * fixture["positions"] * (fixture["hidden_width"] + 1) * fixture["element_bytes"]
    )
    ordinary = Tensor(Storage(2, fixture["chunk_size"] * fixture["vocab"] * 4))
    pack(base)
    pack(ordinary)
    context = SimpleNamespace(
        logits=Tensor(Storage(3, fixture["valid_tokens"] * fixture["vocab"] * 4))
    )
    alias = Tensor(context.logits.storage)
    assert sum(saved.values()) < budget  # Exact old hook path misses ctx.logits.
    observed = {p: size for p, size in census().items() if p not in baseline}
    assert observed[3] == fixture["valid_tokens"] * fixture["vocab"] * 4
    assert sum(observed.values()) > budget
    del alias, context.logits
    assert 3 not in census()  # Census retained metadata, not strong tensor references.
    assert sum(size for p, size in census().items() if p not in baseline) < budget


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_frozen_corpus_excerpts_have_provenance_and_unchanged_content(path):
    case = json.loads(path.read_text())
    assert case["tasks"] and case["provenance"] and case["source"]["url"]
    assert all(len(task["digest"]) == 64 for task in case["tasks"])
    for snippet in case.get("snippets", {}).values():
        assert hashlib.sha256(snippet["text"].encode()).hexdigest() == snippet["text_sha256"]
        assert snippet["path"] and len(snippet["sha256"]) == 64
        assert snippet["selector"]
