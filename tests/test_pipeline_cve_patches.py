"""cve_patches — pipeline contract + helper unit tests.

OSV client tests live in test_osv.py. Validation harness is exercised
via pr_runtime tests (we reuse it verbatim). This file covers:

  - _build_instruction shape
  - Pipeline contract (requires_bootstrap, missing-bootstrap rejection)
"""

from __future__ import annotations

import pytest

from repo2rlenv.osv import OSVVuln
from repo2rlenv.pipelines.cve_patches import CVEPatchesPipeline, _build_instruction
from repo2rlenv.spec.options import CVEPatchesOptions


def _make_vuln(
    cve: str = "CVE-2024-1234",
    severity: str = "HIGH",
    cwe: list[str] | None = None,
) -> OSVVuln:
    return OSVVuln(
        id="GHSA-abc-def-ghi",
        aliases=[cve],
        summary="Brief description of the issue",
        details="Detailed exposition of the vulnerability.",
        severity_text=severity,
        cwe_ids=cwe or ["CWE-22"],
        published="2024-09-12T00:00:00Z",
    )


def test_instruction_omits_vuln_id_but_keeps_class_and_symptom():
    # The CVE/GHSA id is a direct lookup key to the published patch — it must
    # NOT appear in the visible prompt (it lives in task.toml metadata).
    out = _build_instruction(_make_vuln())
    assert "CVE-2024-1234" not in out
    assert "**Severity:** HIGH" in out
    assert "CWE-22" in out  # the vulnerability *class* is kept — useful, not a fix-pointer
    assert "Detailed exposition" in out


def test_instruction_falls_back_when_no_cve():
    v = OSVVuln(id="GHSA-only", aliases=[], summary="x", details="y", severity_text="LOW")
    out = _build_instruction(v)
    assert "GHSA-only" not in out  # id scrubbed; generic title used instead
    assert "Security advisory" in out


def test_instruction_handles_empty_details():
    v = OSVVuln(id="GHSA-x", summary="brief", severity_text="MEDIUM")
    out = _build_instruction(v)
    assert "no detailed description" in out


# ---------------------------------------------------------------------------
# Pipeline contract
# ---------------------------------------------------------------------------


def test_cve_patches_requires_bootstrap_attr():
    assert CVEPatchesPipeline.requires_bootstrap is True


def test_cve_patches_rejects_missing_bootstrap():
    from repo2rlenv.spec.input import (
        GenerationInput,
        LLMSpec,
        OutputSpec,
        PipelineName,
        PipelineSpec,
        RepoSpec,
    )

    gen_input = GenerationInput(
        repo=RepoSpec(url="pallets/werkzeug"),
        pipeline=PipelineSpec(name=PipelineName.CVE_PATCHES, options={}),
        llm=LLMSpec(provider="anthropic", model="claude-sonnet-4-6"),
        output=OutputSpec(destination="./out", org="x", dataset_name="y"),
    )
    with pytest.raises(RuntimeError, match="requires a BootstrapResult"):
        CVEPatchesPipeline(gen_input, CVEPatchesOptions(), bootstrap=None)


def test_cve_patches_options_defaults():
    opts = CVEPatchesOptions()
    assert opts.limit == 50
    assert opts.min_severity == "low"
    # PoC-test synthesis is on by default → we demand a real F2P oracle.
    assert opts.synthesize_poc_test is True
    assert opts.require_fail_to_pass is True
    assert opts.max_pass_to_pass == 50
    assert opts.osv_ecosystem is None
    assert opts.osv_package is None


def test_poc_test_patch_is_new_file_git_diff():
    """Synthesized PoC code is wrapped as a git-appliable new-file test diff."""
    from repo2rlenv.pipelines.cve_patches import CVEPatchesPipeline

    code = "from werkzeug.utils import safe_join\n\n\ndef test_traversal():\n    assert safe_join('/a', '../b') is None\n"
    diff = CVEPatchesPipeline._poc_test_patch(code, "CVE-2020-1234")
    assert "diff --git a/tests/test_cve_cve_2020_1234.py b/tests/test_cve_cve_2020_1234.py" in diff
    assert "new file mode" in diff
    assert "+def test_traversal():" in diff


def test_instruction_strips_fix_leaks():
    """The visible instruction must not leak fix-pointers (PR/commit/CVE id/version)."""
    v = OSVVuln(
        id="GHSA-248m-82v9-q6g6",
        aliases=["CVE-2026-48156"],
        summary="Long runtimes for zero-only /W width values in cross-reference streams",
        details=(
            "A crafted PDF causes a Denial of Service.\n\n"
            "## Workarounds\n\n"
            "Apply the changes from PR https://github.com/py-pdf/pypdf/pull/3791 "
            "(commit 507d7c9aa6ea83389b954b9c3c0c528fe5d5da70).\n\n"
            "## References\n- CVE-2026-48156\n\n"
            "Fixed in version 6.1.1. Please upgrade to the latest release."
        ),
        cwe_ids=["CWE-834"],
        severity_text="MODERATE",
    )
    out = _build_instruction(v)
    for leak in [
        "3791",
        "507d7c9",
        "CVE-2026-48156",
        "GHSA-248m",
        "github.com",
        "6.1.1",
        "Workarounds",
        "References",
        "upgrade",
    ]:
        assert leak not in out, f"leak {leak!r} survived scrubbing"
    # symptom + CWE class are kept (that's the task signal)
    assert "cross-reference streams" in out
    assert "Denial of Service" in out
    assert "CWE-834" in out


def test_strip_fix_leaks_keeps_plain_description():
    from repo2rlenv.pipelines.cve_patches import _strip_fix_leaks

    txt = "Improper validation lets an attacker bypass the auth check via a crafted header."
    assert _strip_fix_leaks(txt) == txt


def test_poc_user_prompt_includes_cve_and_diff():
    from repo2rlenv.osv import OSVVuln
    from repo2rlenv.pipelines.cve_patches import _poc_user_prompt

    v = OSVVuln(
        id="GHSA-xxxx",
        aliases=["CVE-2020-1234"],  # cve_id is derived from id/aliases
        summary="Path traversal in safe_join",
        details="safe_join fails to reject ../",
        cwe_ids=["CWE-22"],
        severity_text="high",
        published="2020-01-01",
    )
    assert v.cve_id == "CVE-2020-1234"
    p = _poc_user_prompt(v, "diff --git a/x b/x\n+fix\n")
    assert "CVE-2020-1234" in p and "safe_join" in p and "diff --git" in p
