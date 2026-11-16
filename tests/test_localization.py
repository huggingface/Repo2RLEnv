"""Localization mission: report-only sibling of a validated remediation task (RFC 0035).

Covers the pure pieces (old-side hunk ranges, path redaction, the stdlib verifier),
the task builder + the real emitter, and `validate --deep` on what we emit. The
Docker/Harbor run of a `-loc` task is opt-in elsewhere; here the verifier runs as a
subprocess exactly as `tests/test.sh` invokes it (`python -B -I -S`).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import tomli_w

from repo2rlenv.emitter.harbor import HarborTask, write_harbor_task
from repo2rlenv.pipelines._env_guard import egress_guard_compose
from repo2rlenv.pipelines._localization import (
    LOCALIZATION_SUFFIX,
    REPORT_PATH,
    SKIP_NO_HUNKS,
    SKIP_TOO_BROAD,
    build_instruction,
    build_localization_task,
    build_oracle,
    changed_regions,
    emit_missions,
    is_localizable,
    old_hunk_ranges,
    redact_paths,
    symptom_of,
)
from repo2rlenv.pipelines.pr_runtime import (
    MIN_HARBOR_VERSION,
    WORKSPACE_ARTIFACT,
    _runtime_aux_files,
    build_environment_dockerfile,
    build_eval_script,
    separate_verifier_files,
    verifier_config,
)
from repo2rlenv.spec.options import (
    CommitRuntimeOptions,
    CVEPatchesOptions,
    PRRuntimeOptions,
)
from repo2rlenv.validation import validate_task

BASE = "a" * 40
F2P = ["tests/test_parse.py::test_empty"]
P2P = ["tests/test_parse.py::test_ok"]

PATCH = """\
diff --git a/src/pkg/parser.py b/src/pkg/parser.py
index 1111111..2222222 100644
--- a/src/pkg/parser.py
+++ b/src/pkg/parser.py
@@ -10,2 +10,4 @@ def parse(text):
     tokens = text.split()
-    return tokens[0]
+    if not tokens:
+        return None
+    return tokens[0]
@@ -40,3 +41,3 @@ def other():
     pass
-    x = 1
+    x = 2
     pass
diff --git a/src/pkg/new_module.py b/src/pkg/new_module.py
new file mode 100644
--- /dev/null
+++ b/src/pkg/new_module.py
@@ -0,0 +1,2 @@
+def added():
+    return 1
"""

ISSUE = (
    "# Issue\n\n**Title:** parse() crashes on empty input\n\n## Description\n\n"
    "Calling parse('') raises IndexError. The problem is in src/pkg/parser.py, "
    "inside parser.py's parse().\n\n## Task\n\nModify the repository so that the issue "
    "described above is resolved."
)


def parser_source() -> str:
    """A file whose lines 11 and 41 are the ones PATCH changes; every line is unique."""
    lines = [f"# filler line {n} of the parser module, kept distinct" for n in range(1, 61)]
    lines[8] = "def parse(text):"
    lines[9] = "    tokens = text.split()"
    lines[10] = "    return tokens[0]"
    lines[39] = "    pass"
    lines[40] = "    x = 1"
    lines[41] = "    pass"
    return "\n".join(lines) + "\n"


PARSER_SOURCE = parser_source()


def _remediation_task() -> HarborTask:
    return HarborTask(
        name="owner__repo-7",
        org="org",
        description="parse() crashes on empty input",
        instruction=ISSUE,
        oracle_diff=PATCH,
        repo2env={
            "pipeline": "pr_runtime",
            "repo": "owner/repo",
            "ref": BASE,
            "reward_kinds": ["test_execution", "diff_similarity"],
            "pr_runtime": {
                "fail_to_pass": F2P,
                "pass_to_pass": P2P,
                "reward_mode": "graded",
                "harbor_min_version": MIN_HARBOR_VERSION,
            },
            "reward_calibration": {"f2p_count": 1, "p2p_count": 1},
        },
        keywords=["repo", "pr_runtime"],
        environment_dockerfile=build_environment_dockerfile("local/r2e-bootstrap/o__r:abc", BASE),
        test_script=build_eval_script(BASE, "", ["pytest -q"], fail_to_pass=F2P, pass_to_pass=P2P),
        verifier=verifier_config(None),
        artifacts=WORKSPACE_ARTIFACT,
        aux_files={
            **_runtime_aux_files(F2P, P2P),
            **separate_verifier_files(),
            "environment/docker-compose.yaml": egress_guard_compose(),
        },
    )


def _load(task_dir: Path) -> dict:
    return tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------
# Options
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("model", [PRRuntimeOptions, CommitRuntimeOptions, CVEPatchesOptions])
def test_mission_defaults_to_remediation(model):
    assert model().mission == "remediation"


@pytest.mark.parametrize("model", [PRRuntimeOptions, CommitRuntimeOptions, CVEPatchesOptions])
def test_mission_accepts_the_three_values_and_rejects_others(model):
    for value in ("remediation", "localization", "both"):
        assert model(mission=value).mission == value
    with pytest.raises(ValueError):
        model(mission="patch")


# ----------------------------------------------------------------------------
# Old-side hunks, redaction, instruction
# ----------------------------------------------------------------------------


def test_old_hunk_ranges_use_the_base_side_and_skip_created_files():
    ranges = old_hunk_ranges(PATCH)
    assert ranges == {"src/pkg/parser.py": [(10, 11), (40, 42)]}
    assert "src/pkg/new_module.py" not in ranges


def test_old_hunk_ranges_pure_insertion_points_at_the_line_it_follows():
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -5,0 +6,2 @@ ctx\n+x = 1\n+y = 2\n"
    assert old_hunk_ranges(diff) == {"a.py": [(5, 5)]}


def test_old_hunk_ranges_single_line_hunk_without_count():
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -3 +3 @@\n-a\n+b\n"
    assert old_hunk_ranges(diff) == {"a.py": [(3, 3)]}


def test_old_hunk_ranges_empty_diff():
    assert old_hunk_ranges("") == {}


def test_changed_regions_exclude_context_lines():
    # PATCH's first hunk spans old lines 10-11 but only line 11 is removed/replaced.
    assert changed_regions(PATCH) == {"src/pkg/parser.py": [(11, 11), (41, 41)]}


def test_changed_regions_pure_insertion_is_the_line_it_follows():
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -5,0 +6,2 @@ ctx\n+x = 1\n+y = 2\n"
    assert changed_regions(diff) == {"a.py": [(5, 5)]}


def test_changed_regions_merge_nearby_lines_but_not_distant_ones():
    # Old lines: 1 (changed), 2-4 (context), 5 (changed), 6-25 (context), 26 (changed).
    context = lambda n: "".join(f" c{i}\n" for i in range(n))  # noqa: E731
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,26 +1,26 @@\n"
        f"-r1\n+n1\n{context(3)}-r5\n+n5\n{context(20)}-far\n+n\n"
    )
    # Lines 1 and 5 are 4 apart (within the merge gap) and become one region; 26 stands alone.
    assert changed_regions(diff) == {"a.py": [(1, 5), (26, 26)]}


def test_changed_regions_survive_a_removed_line_that_looks_like_a_file_header():
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,2 @@\n"
        " keep\n--- a SQL comment\n ok\n"
    )
    assert changed_regions(diff) == {"a.py": [(2, 2)]}


def test_changed_regions_tolerate_a_hunk_header_with_wrong_counts():
    # The first header claims 9 old lines; the body has 2. The next hunk must still be read.
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,9 +1,9 @@\n ctx\n-old\n+new\n"
        "@@ -20,2 +20,2 @@\n ctx\n-old2\n+new2\n"
    )
    assert changed_regions(diff) == {"a.py": [(2, 2), (21, 21)]}


def test_changed_regions_empty_and_created_files():
    assert changed_regions("") == {}
    created = "diff --git a/n.py b/n.py\n--- /dev/null\n+++ b/n.py\n@@ -0,0 +1 @@\n+x\n"
    assert changed_regions(created) == {}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/pkg/parser.py", True),
        ("pyproject.toml", True),
        ("CHANGES.md", False),
        ("docs/guide.py", False),
        (".github/workflows/ci.yml", False),
        ("uv.lock", False),
        ("README.rst", False),
        ("src/changelog.py", False),
        ("src/docs_helper.py", True),
    ],
)
def test_is_localizable(path: str, expected: bool):
    assert is_localizable(path) is expected


def test_build_oracle_keeps_only_code_files_and_graded_regions():
    docs_and_code = PATCH + (
        "diff --git a/CHANGES.md b/CHANGES.md\n--- a/CHANGES.md\n+++ b/CHANGES.md\n"
        "@@ -1,2 +1,3 @@\n x\n+note\n y\n"
    )
    oracle, reason = build_oracle(docs_and_code)
    assert reason is None and oracle is not None
    assert oracle["files"] == ["src/pkg/parser.py"]
    assert oracle["hunks"] == [
        {"path": "src/pkg/parser.py", "start": 11, "end": 11},
        {"path": "src/pkg/parser.py", "start": 41, "end": 41},
    ]


def test_build_oracle_docs_only_fix_has_nothing_to_localize():
    docs_only = (
        "diff --git a/CHANGES.md b/CHANGES.md\n--- a/CHANGES.md\n+++ b/CHANGES.md\n"
        "@@ -1,2 +1,3 @@\n x\n+note\n y\n"
    )
    assert build_oracle(docs_only) == (None, SKIP_NO_HUNKS)


def test_build_oracle_a_fix_spread_over_too_many_files_is_too_broad():
    many = "".join(
        f"diff --git a/m{n}.py b/m{n}.py\n--- a/m{n}.py\n+++ b/m{n}.py\n@@ -1 +1 @@\n-a\n+b\n"
        for n in range(25)
    )
    assert build_oracle(many) == (None, SKIP_TOO_BROAD)


def test_redact_paths_hides_full_paths_and_bare_names_but_not_prose():
    text = "See src/pkg/parser.py and parser.py; the parser is slow. Also xparser.py."
    out = redact_paths(text, ["src/pkg/parser.py"])
    assert "src/pkg/parser.py" not in out
    assert "parser.py;" not in out
    assert out.count("[file]") == 2
    assert "the parser is slow" in out
    assert "xparser.py" in out  # a different file is not redacted


def test_symptom_drops_the_remediation_task_section():
    assert "## Task" not in symptom_of(ISSUE)
    assert "parse('')" in symptom_of(ISSUE)


def test_instruction_forbids_patching_names_the_report_and_leaks_no_gold_path():
    text = build_instruction(ISSUE, ["src/pkg/parser.py"], BASE)
    assert REPORT_PATH in text
    assert "Do not modify the repository" in text
    assert BASE[:12] in text
    assert "parser.py" not in text
    assert "Modify the repository so that" not in text


# ----------------------------------------------------------------------------
# The task builder + the real emitter
# ----------------------------------------------------------------------------


def _loc(task: HarborTask | None = None) -> HarborTask:
    built, reason = build_localization_task(
        task or _remediation_task(), patch=PATCH, base_commit=BASE
    )
    assert reason is None and built is not None
    return built


def test_localization_task_is_a_distinct_sibling_with_its_own_reward_kind():
    base, loc = _remediation_task(), _loc()
    assert loc.name == f"{base.name}{LOCALIZATION_SUFFIX}" != base.name
    assert loc.repo2env["mission"] == "localization"
    assert loc.repo2env["reward_kinds"] == ["localization"]
    assert "pr_runtime" not in loc.repo2env  # the F2P/P2P subtable describes the patch task
    assert "reward_calibration" not in loc.repo2env
    assert loc.repo2env["localization"]["source_task"] == base.name
    assert loc.repo2env["localization"]["harbor_min_version"] == MIN_HARBOR_VERSION
    meta = loc.repo2env["localization"]
    assert meta["grading"] == "oracle" and meta["judge"] == "optional"
    assert (meta["gold_files"], meta["gold_regions"]) == (1, 2)
    assert meta["file_weight"] + meta["region_weight"] == 1.0
    assert base.repo2env["reward_kinds"] == ["test_execution", "diff_similarity"]  # untouched


def test_localization_task_keeps_the_scrubbed_environment_and_the_egress_guard():
    base, loc = _remediation_task(), _loc()
    dockerfile = loc.environment_dockerfile or ""
    # The remediation task's image, untouched, with only the tmux layer added after it.
    assert dockerfile.startswith((base.environment_dockerfile or "").rstrip("\n"))
    assert "tmux" in dockerfile and "/opt/r2e/pristine" in dockerfile
    assert "tmux" not in (base.environment_dockerfile or "")
    assert loc.aux_files["environment/docker-compose.yaml"] == egress_guard_compose()


def test_localization_task_ships_no_f2p_launcher_or_patch_oracle_to_the_verifier():
    loc = _loc()
    shipped = set(loc.aux_files)
    assert {"tests/verifier.py", "tests/grader_tools.py", "tests/oracle.json"} <= shipped
    assert not shipped & {"tests/f2p.json", "tests/p2p.json", "tests/launcher.py"}
    assert "def evaluate" in loc.aux_files["tests/verifier.py"]
    # The judge reads the instruction from tests/, the only directory mounted at verify time.
    assert loc.aux_files["tests/instruction.md"] == loc.instruction


def test_the_oracle_is_the_changed_regions_and_only_ships_at_verification_time():
    loc = _loc()
    oracle = json.loads(loc.aux_files["tests/oracle.json"])
    assert oracle == {
        "files": ["src/pkg/parser.py"],
        "hunks": [
            {"path": "src/pkg/parser.py", "start": 11, "end": 11},
            {"path": "src/pkg/parser.py", "start": 41, "end": 41},
        ],
    }
    assert not [p for p in loc.aux_files if p.startswith("environment/") and "oracle" in p]
    assert "oracle.json" not in (loc.environment_dockerfile or "")


def test_localization_grades_a_report_artifact_in_a_separate_verifier():
    loc = _loc()
    assert loc.artifacts == [{"source": REPORT_PATH}]
    assert loc.verifier["environment_mode"] == "separate"
    assert loc.verifier["timeout_sec"] == 300.0
    script = loc.test_script or ""
    assert "-B -I -S" in script and REPORT_PATH in script
    assert "r2e_finish graded missing_report" in script
    assert "r2e_finish ungraded verifier_error" in script
    assert '--oracle "$SCRIPT_DIR/oracle.json"' in script
    assert '--instruction "$SCRIPT_DIR/instruction.md"' in script


def test_no_gold_path_reaches_the_agent_image_or_the_instruction():
    loc = _loc()
    for leaked in ("parser.py", "new_module.py", "return tokens[0]"):
        assert leaked not in loc.instruction
        assert leaked not in (loc.environment_dockerfile or "")


def test_the_oracle_is_in_tests_and_solution_never_in_environment(tmp_path: Path):
    task_dir = write_harbor_task(_loc(), tmp_path)
    assert (task_dir / "tests" / "oracle.json").is_file()
    assert (task_dir / "solution" / "oracle.json").is_file()
    assert not list((task_dir / "environment").rglob("oracle*"))
    assert not (task_dir / "solution" / "gold.json").exists()


def test_docs_only_fix_has_nothing_to_localize():
    docs_only = (
        "diff --git a/CHANGES.md b/CHANGES.md\n--- a/CHANGES.md\n+++ b/CHANGES.md\n"
        "@@ -1,2 +1,3 @@\n x\n+note\n y\n"
    )
    task, reason = build_localization_task(_remediation_task(), patch=docs_only, base_commit=BASE)
    assert task is None and reason == SKIP_NO_HUNKS


def test_created_files_only_fix_has_nothing_to_localize():
    task, reason = build_localization_task(
        _remediation_task(),
        patch=(
            "diff --git a/n.py b/n.py\nnew file mode 100644\n--- /dev/null\n+++ b/n.py\n"
            "@@ -0,0 +1 @@\n+x = 1\n"
        ),
        base_commit=BASE,
    )
    assert task is None and reason == SKIP_NO_HUNKS


def test_emitter_writes_a_report_solver_not_git_apply(tmp_path: Path):
    task_dir = write_harbor_task(_loc(), tmp_path)
    solve = task_dir / "solution" / "solve.sh"
    assert solve.stat().st_mode & stat.S_IXUSR
    text = solve.read_text()
    assert "solve.py" in text and "git apply" not in text
    assert "solve.py" in {p.name for p in (task_dir / "solution").iterdir()}
    assert (task_dir / "tests" / "test.sh").stat().st_mode & stat.S_IXUSR


def test_remediation_solve_sh_is_unchanged(tmp_path: Path):
    task_dir = write_harbor_task(_remediation_task(), tmp_path)
    assert "git apply" in (task_dir / "solution" / "solve.sh").read_text()


# ----------------------------------------------------------------------------
# emit_missions
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mission", "names"),
    [
        ("remediation", ["owner__repo-7"]),
        ("localization", ["owner__repo-7-loc"]),
        ("both", ["owner__repo-7", "owner__repo-7-loc"]),
    ],
)
def test_emit_missions_writes_what_was_asked(tmp_path: Path, mission: str, names: list[str]):
    written, reason = emit_missions(
        _remediation_task(), tmp_path, mission=mission, patch=PATCH, base_commit=BASE
    )
    assert written == names and reason is None
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(names)


def test_emit_missions_localization_only_skips_when_nothing_to_localize(tmp_path: Path):
    created = "diff --git a/n.py b/n.py\n--- /dev/null\n+++ b/n.py\n@@ -0,0 +1 @@\n+x\n"
    written, reason = emit_missions(
        _remediation_task(), tmp_path, mission="localization", patch=created, base_commit=BASE
    )
    assert written == [] and reason == SKIP_NO_HUNKS
    assert list(tmp_path.iterdir()) == []


def test_emit_missions_both_still_emits_remediation_when_nothing_to_localize(tmp_path: Path):
    created = "diff --git a/n.py b/n.py\n--- /dev/null\n+++ b/n.py\n@@ -0,0 +1 @@\n+x\n"
    written, _ = emit_missions(
        _remediation_task(), tmp_path, mission="both", patch=created, base_commit=BASE
    )
    assert written == ["owner__repo-7"]


# ----------------------------------------------------------------------------
# validate --deep on what we emit
# ----------------------------------------------------------------------------


def _findings(task_dir: Path):
    return validate_task(task_dir, _load(task_dir), oracle=True)


def test_emitted_localization_task_validates_clean(tmp_path: Path):
    task_dir = write_harbor_task(_loc(), tmp_path)
    assert _findings(task_dir) == []


def test_template_graded_task_still_warns(tmp_path: Path):
    task_dir = write_harbor_task(_loc(), tmp_path)
    data = _load(task_dir)
    data["metadata"]["repo2env"]["localization"]["grading"] = "template"
    (task_dir / "task.toml").write_bytes(tomli_w.dumps(data).encode("utf-8"))
    assert [f for f in _findings(task_dir) if f.severity == "warning" and "template" in f.message]


def test_oracle_graded_task_without_an_oracle_is_an_error(tmp_path: Path):
    task_dir = write_harbor_task(_loc(), tmp_path)
    os.unlink(task_dir / "tests" / "oracle.json")
    assert any(f.severity == "error" and "oracle.json" in f.path for f in _findings(task_dir))


@pytest.mark.parametrize(
    "broken",
    [
        "not json",
        '{"files": [], "hunks": []}',
        '{"files": ["a.py"], "hunks": [{"path": "b.py", "start": 1, "end": 2}]}',
        '{"files": ["a.py"], "hunks": [{"path": "a.py", "start": 5, "end": 2}]}',
        '{"files": ["a.py"], "hunks": [{"path": "a.py", "start": 0, "end": 2}]}',
    ],
)
def test_malformed_oracle_is_an_error(tmp_path: Path, broken: str):
    task_dir = write_harbor_task(_loc(), tmp_path)
    (task_dir / "tests" / "oracle.json").write_text(broken, encoding="utf-8")
    assert any(f.severity == "error" and "oracle.json" in f.path for f in _findings(task_dir))


def test_an_oracle_in_the_agent_image_is_an_error(tmp_path: Path):
    task_dir = write_harbor_task(_loc(), tmp_path)
    (task_dir / "environment" / "oracle.json").write_text("{}", encoding="utf-8")
    assert any(f.severity == "error" and f.path == "environment" for f in _findings(task_dir))


def test_an_instruction_naming_a_gold_file_is_an_error(tmp_path: Path):
    task_dir = write_harbor_task(_loc(), tmp_path)
    (task_dir / "instruction.md").write_text("look in src/pkg/parser.py", encoding="utf-8")
    assert any(f.severity == "error" and f.path == "instruction.md" for f in _findings(task_dir))


def test_localization_task_without_report_artifact_is_an_error(tmp_path: Path):
    task_dir = write_harbor_task(_loc(), tmp_path)
    data = _load(task_dir)
    data["artifacts"] = [{"source": "/workspace/other.md"}]  # report_path still says FINDINGS.md
    (task_dir / "task.toml").write_bytes(tomli_w.dumps(data).encode("utf-8"))
    messages = [f.message for f in _findings(task_dir) if f.severity == "error"]
    assert any("report_path" in m for m in messages)


def test_localization_task_missing_verifier_is_an_error(tmp_path: Path):
    task_dir = write_harbor_task(_loc(), tmp_path)
    os.unlink(task_dir / "tests" / "verifier.py")
    assert any(f.severity == "error" and "verifier.py" in f.path for f in _findings(task_dir))


def test_remediation_task_still_validates_clean_next_to_its_sibling(tmp_path: Path):
    write_harbor_task(_loc(), tmp_path)
    task_dir = write_harbor_task(_remediation_task(), tmp_path)
    assert _findings(task_dir) == []


# ----------------------------------------------------------------------------
# The reference solver must pass its own verifier
# ----------------------------------------------------------------------------


def _run_solver_and_verifier(
    tmp_path: Path, source: str
) -> tuple[subprocess.CompletedProcess, dict]:
    """Run the emitted solve.sh on a tree, then grade its report with the shipped verifier.

    ``/workspace`` is redirected to a temp tree for the solver; the verifier gets a SEPARATE
    copy as ``--pristine``, as in the real image (the agent's tree is not the grading tree).
    """
    task_dir = write_harbor_task(_loc(), tmp_path / "out")
    workspace = tmp_path / "workspace"
    (workspace / "src" / "pkg").mkdir(parents=True)
    (workspace / "src" / "pkg" / "parser.py").write_text(source, encoding="utf-8")
    pristine = tmp_path / "pristine"
    shutil.copytree(workspace, pristine)
    solution = task_dir / "solution"
    solve = solution / "solve.sh"
    solve.write_text(solve.read_text().replace("/workspace", str(workspace)), encoding="utf-8")
    ran = subprocess.run(["bash", str(solve)], capture_output=True, text=True, check=False)
    logs = tmp_path / "logs"
    logs.mkdir()
    graded = subprocess.run(
        [
            sys.executable,
            "-B",
            "-I",
            "-S",
            str(task_dir / "tests" / "verifier.py"),
            "--report",
            str(workspace / "FINDINGS.md"),
            "--pristine",
            str(pristine),
            "--oracle",
            str(task_dir / "tests" / "oracle.json"),
            "--instruction",
            str(task_dir / "tests" / "instruction.md"),
            "--out",
            str(logs),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert graded.returncode == 0, graded.stderr
    return ran, json.loads((logs / "reward-details.json").read_text())


def test_reference_solution_scores_one_on_its_own_verifier(tmp_path: Path):
    ran, details = _run_solver_and_verifier(tmp_path, PARSER_SOURCE)
    assert ran.returncode == 0, ran.stderr
    assert details["reward"] == 1.0, details
    assert details["resolved"] is True
    assert details["regions_hit"] == details["regions_total"] == 2


def test_reference_solution_works_when_the_changed_region_is_at_the_file_edge(tmp_path: Path):
    ran, details = _run_solver_and_verifier(tmp_path, "def parse(text):\n    return text\n")
    # The oracle's lines do not exist in this file, so no unique quotation can be found:
    # the solver must say so (exit 2) rather than write a report that points nowhere.
    assert ran.returncode == 2 and "no unique quotation" in ran.stderr
    assert details["reward"] == 0.0


def test_solution_dir_ships_the_verifier_as_locate_py(tmp_path: Path):
    task_dir = write_harbor_task(_loc(), tmp_path)
    assert (task_dir / "solution" / "locate.py").read_text() == (
        task_dir / "tests" / "verifier.py"
    ).read_text()


# ----------------------------------------------------------------------------
# pr_diff: a task with no separate verifier of its own
# ----------------------------------------------------------------------------

DIFF_WITH_TESTS = (
    PATCH
    + """\
diff --git a/tests/test_parse.py b/tests/test_parse.py
--- a/tests/test_parse.py
+++ b/tests/test_parse.py
@@ -1,2 +1,4 @@
 def test_ok():
     assert parse("a") == "a"
+def test_empty():
+    assert parse("") is None
"""
)


def _pr_diff_task():
    from repo2rlenv.github import PullRequestSummary
    from repo2rlenv.pipelines import pr_diff

    pr = PullRequestSummary(
        number=7,
        title="Fix parse crash",
        body="parse('') raises IndexError in src/pkg/parser.py.",
        state="closed",
        merged_at="2026-01-01T00:00:00Z",
        base_ref="main",
        base_sha=BASE,
        head_sha="b" * 40,
        is_draft=False,
        url="https://github.com/owner/repo/pull/7",
        changed_files=["src/pkg/parser.py", "tests/test_parse.py"],
    )
    return HarborTask(
        name="owner__repo-7",
        org="org",
        description=pr.title,
        instruction=pr_diff._build_instruction(pr),
        oracle_diff=DIFF_WITH_TESTS,
        repo2env={
            "pipeline": "pr_diff",
            "repo": "owner/repo",
            "ref": BASE,
            "reward_kinds": ["diff_similarity"],
            "pr_diff": {"context_files": pr.changed_files},
            "reward_calibration": {"baseline_reward": 0.1, "loc_changed": 9},
        },
        keywords=["repo", "pr_diff"],
        environment_dockerfile=pr_diff.build_pr_diff_environment_dockerfile(
            repo_url="https://github.com/owner/repo", base_commit=BASE
        ),
        test_script=pr_diff.build_pr_diff_eval_script(base_commit=BASE),
        aux_files=pr_diff._pr_diff_aux_files(oracle_diff=DIFF_WITH_TESTS, instruction="x"),
    )


def _source_half(diff: str) -> str:
    from repo2rlenv.pipelines.pr_runtime import split_patch_and_test_patch

    return split_patch_and_test_patch(diff)[0]


def _pr_diff_loc() -> HarborTask:
    built, reason = build_localization_task(
        _pr_diff_task(), patch=_source_half(DIFF_WITH_TESTS), base_commit=BASE, stash_tree=True
    )
    assert reason is None and built is not None
    return built


def test_pr_diff_options_accept_mission_and_default_to_remediation():
    from repo2rlenv.spec.options import PRDiffOptions

    assert PRDiffOptions().mission == "remediation"
    assert PRDiffOptions(mission="both").mission == "both"
    with pytest.raises(ValueError):
        PRDiffOptions(mission="patch")


def test_pr_diff_mission_needs_an_environment():
    from repo2rlenv.spec.options import PRDiffOptions

    with pytest.raises(ValueError, match="emit_harbor_env"):
        PRDiffOptions(mission="localization", emit_harbor_env=False)
    assert PRDiffOptions(emit_harbor_env=False).mission == "remediation"


def test_pr_diff_sibling_gets_the_pristine_copy_the_base_image_lacks():
    base = _pr_diff_task()
    assert "/opt/r2e/pristine" not in (base.environment_dockerfile or "")
    loc = _pr_diff_loc()
    dockerfile = loc.environment_dockerfile or ""
    assert dockerfile.startswith((base.environment_dockerfile or "").rstrip("\n"))
    assert "cp -a /workspace /opt/r2e/pristine" in dockerfile
    # The history scrub and the base-commit reset stay before the copy.
    assert dockerfile.index("git reset --hard") < dockerfile.index("/opt/r2e/pristine")


def test_pr_diff_sibling_is_a_separate_verifier_task_with_no_diff_oracle_in_tests():
    loc = _pr_diff_loc()
    shipped = set(loc.aux_files)
    assert {"tests/verifier.py", "tests/grader_tools.py", "tests/launcher.py"} <= shipped
    # None of pr_diff's own grading material: oracle.patch is the answer to remediation.
    assert not shipped & {"tests/oracle.patch", "tests/capture.py"}
    # tests/instruction.md is the localization instruction, not pr_diff's remediation copy.
    assert loc.aux_files["tests/instruction.md"] == loc.instruction
    assert "tests/oracle.json" in shipped
    assert "def evaluate" in loc.aux_files["tests/verifier.py"]
    assert loc.verifier["environment_mode"] == "separate"
    assert loc.artifacts == [{"source": REPORT_PATH}]
    assert "pr_diff" not in loc.repo2env  # the diff-similarity subtable is not this task's
    assert loc.repo2env["reward_kinds"] == ["localization"]
    assert "reward_calibration" not in loc.repo2env


def test_pr_diff_sibling_keeps_the_egress_guard():
    compose = _pr_diff_loc().aux_files["environment/docker-compose.yaml"]
    assert "github.com" in compose and "pypi.org" in compose


def test_pr_diff_gold_is_the_source_half_not_the_tests():
    loc = _pr_diff_loc()
    assert "tests/test_parse.py" not in loc.oracle_diff
    assert "src/pkg/parser.py" in loc.oracle_diff
    for leaked in ("parser.py", "test_parse", "test_empty"):
        assert leaked not in loc.instruction
        assert leaked not in (loc.environment_dockerfile or "")


def test_pr_diff_instruction_has_its_task_section_replaced():
    loc = _pr_diff_loc()
    assert "Modify the repository" not in loc.instruction
    assert "SWE-RL" not in loc.instruction  # pr_diff's reward is not this task's
    assert "Do not modify the repository" in loc.instruction
    assert "IndexError" in loc.instruction  # the symptom survives


@pytest.mark.parametrize(
    ("mission", "names"),
    [
        ("remediation", ["owner__repo-7"]),
        ("localization", ["owner__repo-7-loc"]),
        ("both", ["owner__repo-7", "owner__repo-7-loc"]),
    ],
)
def test_pr_diff_emit_missions(tmp_path: Path, mission: str, names: list[str]):
    written, reason = emit_missions(
        _pr_diff_task(),
        tmp_path,
        mission=mission,
        patch=_source_half(DIFF_WITH_TESTS),
        base_commit=BASE,
        stash_tree=True,
    )
    assert written == names and reason is None
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(names)


def test_pr_diff_remediation_output_is_untouched_by_the_mission_machinery(tmp_path: Path):
    plain = write_harbor_task(_pr_diff_task(), tmp_path / "plain")
    emit_missions(
        _pr_diff_task(),
        tmp_path / "via",
        mission="both",
        patch=_source_half(DIFF_WITH_TESTS),
        base_commit=BASE,
        stash_tree=True,
    )
    via = tmp_path / "via" / plain.name
    files = sorted(p.relative_to(plain) for p in plain.rglob("*") if p.is_file())
    assert files == sorted(p.relative_to(via) for p in via.rglob("*") if p.is_file())
    for rel in files:
        if rel.name == "task.toml":
            continue  # carries built_at-independent but name-bearing fields; compared below
        assert (plain / rel).read_bytes() == (via / rel).read_bytes(), rel
    assert _load(plain)["metadata"]["repo2env"]["reward_kinds"] == ["diff_similarity"]


def test_pr_diff_test_only_source_half_is_nothing_to_localize(tmp_path: Path):
    tests_only = DIFF_WITH_TESTS.split("diff --git a/tests/test_parse.py")[1]
    tests_only = "diff --git a/tests/test_parse.py" + tests_only
    written, reason = emit_missions(
        _pr_diff_task(),
        tmp_path,
        mission="localization",
        patch=_source_half(tests_only),
        base_commit=BASE,
        stash_tree=True,
    )
    assert written == [] and reason == SKIP_NO_HUNKS


def test_emitted_pr_diff_localization_task_validates_clean(tmp_path: Path):
    emit_missions(
        _pr_diff_task(),
        tmp_path,
        mission="both",
        patch=_source_half(DIFF_WITH_TESTS),
        base_commit=BASE,
        stash_tree=True,
    )
    # The diff-verifier checks (oracle.patch) must not fire on the sibling.
    assert _findings(tmp_path / "owner__repo-7-loc") == []
    assert _findings(tmp_path / "owner__repo-7") == []


# ----------------------------------------------------------------------------
# PRDiffPipeline.run(): the command from the bug report, with the provider stubbed
# ----------------------------------------------------------------------------


class _StubProvider:
    def __init__(self, pr, diff: str):
        self._pr, self._diff = pr, diff

    def list_merged_prs(self, *args, **kwargs):
        return [self._pr]

    def fetch_pr_diff(self, *args, **kwargs):
        return self._diff


def _run_pr_diff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **options):
    from repo2rlenv.pipelines import pr_diff
    from repo2rlenv.spec.input import (
        GenerationInput,
        OutputSpec,
        PipelineName,
        PipelineSpec,
        RepoSpec,
    )
    from repo2rlenv.spec.options import PRDiffOptions

    pr = pr_diff.PullRequestSummary(
        number=7,
        title="Fix parse crash",
        body="parse('') raises IndexError in src/pkg/parser.py. " * 3,
        state="closed",
        merged_at="2026-01-01T00:00:00Z",
        base_ref="main",
        base_sha=BASE,
        head_sha="b" * 40,
        is_draft=False,
        url="https://github.com/owner/repo/pull/7",
        changed_files=["src/pkg/parser.py", "tests/test_parse.py"],
    )
    stub = _StubProvider(pr, DIFF_WITH_TESTS)
    monkeypatch.setattr(pr_diff, "provider_for", lambda repo: stub)
    monkeypatch.setattr(pr_diff, "resolve_repo_token", lambda repo, auth: None)
    monkeypatch.setattr(pr_diff, "_quality_filter", lambda *a, **k: None)
    gen = GenerationInput(
        repo=RepoSpec(url="owner/repo"),
        pipeline=PipelineSpec(name=PipelineName.PR_DIFF, options={}),
        output=OutputSpec(destination=str(tmp_path), org="org", dataset_name="d"),
    )
    pipeline = pr_diff.PRDiffPipeline(gen, PRDiffOptions(limit=10, **options))
    return pipeline.run(tmp_path / "tasks")


def test_pr_diff_run_default_emits_remediation_only(tmp_path: Path, monkeypatch):
    result = _run_pr_diff(tmp_path, monkeypatch)
    assert result.emitted == 1
    assert [p.name for p in (tmp_path / "tasks").iterdir()] == ["owner__repo-7"]


def test_pr_diff_run_mission_both_emits_both_tasks(tmp_path: Path, monkeypatch):
    result = _run_pr_diff(tmp_path, monkeypatch, mission="both")
    assert result.emitted == 2
    assert sorted(p.name for p in (tmp_path / "tasks").iterdir()) == [
        "owner__repo-7",
        "owner__repo-7-loc",
    ]
    loc = tmp_path / "tasks" / "owner__repo-7-loc"
    assert _load(loc)["metadata"]["repo2env"]["mission"] == "localization"
    assert "/opt/r2e/pristine" in (loc / "environment" / "Dockerfile").read_text()


def test_pr_diff_run_mission_localization_emits_only_the_sibling(tmp_path: Path, monkeypatch):
    result = _run_pr_diff(tmp_path, monkeypatch, mission="localization")
    assert result.emitted == 1
    assert [p.name for p in (tmp_path / "tasks").iterdir()] == ["owner__repo-7-loc"]


# ----------------------------------------------------------------------------
# Report shape, shipped controls, offline agent (the GroundPnP-style hardening)
# ----------------------------------------------------------------------------


def test_instruction_asks_for_the_sections_in_order_and_names_the_optional_one():
    from repo2rlenv.pipelines._pr_localization_verifier import REQUIRED_SECTIONS

    text = _loc().instruction
    positions = [text.index(f"## {name}") for name in REQUIRED_SECTIONS]
    assert positions == sorted(positions)
    assert "## unaffected" in text and "in this\norder" in text
    assert "Do not modify the repository" in text


def test_the_instruction_never_requires_the_access_control_only_owner_section():
    assert "## owner" not in _loc().instruction


def test_agent_network_is_offline_by_default(tmp_path: Path):
    data = _load(write_harbor_task(_loc(), tmp_path))
    assert data["agent"]["network_mode"] == "no-network"
    assert data["agent"]["timeout_sec"] == 1800.0
    assert data["metadata"]["repo2env"]["localization"]["agent_network"] == "offline"


def test_offline_can_be_turned_off_for_hosts_without_nft_fib(tmp_path: Path):
    built, _ = build_localization_task(
        _remediation_task(), patch=PATCH, base_commit=BASE, offline=False
    )
    data = _load(write_harbor_task(built, tmp_path))
    assert "network_mode" not in data["agent"]
    assert data["metadata"]["repo2env"]["localization"]["agent_network"] == "egress_guard_only"


def test_remediation_task_agent_table_is_unchanged(tmp_path: Path):
    assert _load(write_harbor_task(_remediation_task(), tmp_path))["agent"] == {
        "timeout_sec": 1800.0
    }


def test_loc_offline_option_defaults_on_for_every_pipeline():
    from repo2rlenv.spec.options import PRDiffOptions

    for model in (PRRuntimeOptions, CommitRuntimeOptions, CVEPatchesOptions, PRDiffOptions):
        assert model().loc_offline is True
        assert model(loc_offline=False).loc_offline is False


def test_the_image_has_tmux_installed_after_the_pristine_copy_without_touching_the_base():
    base, loc = _remediation_task(), _loc()
    assert "tmux" not in (base.environment_dockerfile or "")
    dockerfile = loc.environment_dockerfile or ""
    assert dockerfile.index("tmux") > dockerfile.index("/opt/r2e/pristine")


def test_the_controls_ship_in_tests_shortcuts_and_not_in_the_image(tmp_path: Path):
    from repo2rlenv.pipelines._localization import CONTROL_NAMES

    task_dir = write_harbor_task(_loc(), tmp_path)
    shipped = {p.name for p in (task_dir / "tests" / "shortcuts").iterdir()}
    assert {f"{stem}.sh" for stem, _ in CONTROL_NAMES} <= shipped
    assert {"controls.py", "run_all.sh"} <= shipped
    assert [stem for stem, _ in CONTROL_NAMES][-1].startswith("99_")
    assert not list((task_dir / "environment").rglob("shortcuts"))
    assert "shortcuts" not in (task_dir / "environment" / "Dockerfile").read_text()
    for script in (task_dir / "tests" / "shortcuts").glob("*.sh"):
        assert script.stat().st_mode & stat.S_IXUSR
        assert subprocess.run(["bash", "-n", str(script)]).returncode == 0


def test_the_controls_contain_nothing_specific_to_the_task(tmp_path: Path):
    task_dir = write_harbor_task(_loc(), tmp_path)
    controls = (task_dir / "tests" / "shortcuts" / "controls.py").read_text()
    for specific in ("parser.py", "src/pkg", "tokens[0]", "owner__repo"):
        assert specific not in controls


def test_validation_flags_a_missing_section_in_the_instruction(tmp_path: Path):
    task_dir = write_harbor_task(_loc(), tmp_path)
    text = (task_dir / "instruction.md").read_text().replace("## chain", "## flow")
    (task_dir / "instruction.md").write_text(text, encoding="utf-8")
    assert any(f.severity == "error" and "chain" in f.message for f in _findings(task_dir))


def test_validation_flags_an_offline_claim_the_task_does_not_declare(tmp_path: Path):
    task_dir = write_harbor_task(_loc(), tmp_path)
    data = _load(task_dir)
    del data["agent"]["network_mode"]
    (task_dir / "task.toml").write_bytes(tomli_w.dumps(data).encode("utf-8"))
    assert any(f.severity == "error" and "offline" in f.message for f in _findings(task_dir))


def test_validation_warns_when_the_agent_is_not_offline(tmp_path: Path):
    built, _ = build_localization_task(
        _remediation_task(), patch=PATCH, base_commit=BASE, offline=False
    )
    task_dir = write_harbor_task(built, tmp_path)
    findings = _findings(task_dir)
    assert [f for f in findings if f.severity == "error"] == []
    assert [f for f in findings if "egress guard" in f.message]


def test_validation_warns_when_the_attack_scripts_are_missing(tmp_path: Path):
    import shutil

    task_dir = write_harbor_task(_loc(), tmp_path)
    shutil.rmtree(task_dir / "tests" / "shortcuts")
    assert any(f.severity == "warning" and f.path == "tests/shortcuts" for f in _findings(task_dir))


def test_validation_warns_about_an_image_without_tmux(tmp_path: Path):
    task_dir = write_harbor_task(_loc(), tmp_path)
    docker = task_dir / "environment" / "Dockerfile"
    docker.write_text(docker.read_text().replace("tmux", "tmu_"), encoding="utf-8")
    assert any(f.severity == "warning" and "tmux" in f.message for f in _findings(task_dir))
