"""Every registered pipeline must satisfy the Pipeline Protocol."""

from __future__ import annotations

from repo2rlenv.pipelines import PIPELINES, PipelineResult
from repo2rlenv.spec.input import PipelineName
from repo2rlenv.spec.options import OPTIONS_REGISTRY


def test_pipelines_implement_protocol():
    """Each PIPELINES entry duck-conforms to Pipeline."""
    assert PIPELINES, "PIPELINES registry is empty"
    for name, cls in PIPELINES.items():
        # runtime_checkable Protocol — works on the class itself
        assert isinstance(cls, type), f"{name}: not a class"
        # Each implementation must declare its name
        assert hasattr(cls, "name"), f"{name}: missing class attribute `name`"
        # And the declared name must be the registered name
        declared = cls.name
        assert declared == name, f"{name}: class declares name={declared!r}, registered as {name!r}"


def test_pipeline_names_are_in_enum():
    """Every registered pipeline name is a valid PipelineName enum value."""
    for name in PIPELINES:
        # Will raise ValueError if not in the enum
        PipelineName(name)


def test_options_registry_aligns_with_pipelines():
    """Every registered pipeline has a corresponding Options class."""
    for name in PIPELINES:
        assert name in OPTIONS_REGISTRY, f"pipeline {name!r} has no Options class registered"


def test_pipelines_declare_requires_bootstrap():
    """Each pipeline must declare requires_bootstrap (True/False) — cmd_generate dispatches on it."""
    for name, cls in PIPELINES.items():
        assert hasattr(cls, "requires_bootstrap"), (
            f"{name}: missing class attribute `requires_bootstrap`"
        )
        assert isinstance(cls.requires_bootstrap, bool)


def test_python_only_pipelines_declare_supported_languages():
    """The 4 Python-only pipelines must restrict supported_languages to {PYTHON}.

    Anything else risks the silent-late-failure mode the pre-flight check
    in cmd_generate is designed to prevent.
    """
    from repo2rlenv.bootstrap.spec import LanguageHint

    python_only = {
        "mutation_bugs",
        "code_instruct",
        "equivalence_tests",
        "refactor_synthesis",
    }
    for name in python_only:
        cls = PIPELINES[name]
        supported = getattr(cls, "supported_languages", None)
        assert supported is not None, f"{name}: expected supported_languages={{PYTHON}}"
        assert supported == frozenset({LanguageHint.PYTHON}), (
            f"{name}: expected frozenset({{PYTHON}}), got {supported}"
        )


def test_language_agnostic_pipelines_have_no_restriction():
    """Lite + language-agnostic full pipelines must NOT restrict languages."""
    agnostic = {"pr_diff", "pr_runtime", "pr_stream", "commit_runtime", "cve_patches"}
    for name in agnostic:
        cls = PIPELINES[name]
        supported = getattr(cls, "supported_languages", None)
        assert supported is None, (
            f"{name}: expected supported_languages=None (any language ok), got {supported}"
        )


def test_pipeline_result_shape():
    """PipelineResult has the documented fields."""
    r = PipelineResult(
        candidates=0,
        emitted=0,
        skipped=0,
        out_dir=__import__("pathlib").Path("."),
        skip_reasons={},
    )
    # Slots dataclass — these field names must exist
    for attr in ("candidates", "emitted", "skipped", "out_dir", "skip_reasons"):
        assert hasattr(r, attr), f"PipelineResult missing field {attr}"


def test_protocol_export():
    """Pipeline + PipelineResult are public exports of the package."""
    from repo2rlenv import pipelines

    assert hasattr(pipelines, "Pipeline")
    assert hasattr(pipelines, "PipelineResult")
