"""Unit tests for the pr_to_env pipeline.

Pure-Python bits only — real Docker runs are covered by manual end-to-end.
Focus areas:
  * URL parsing (github.com/*/pull/N + gitlab.com MR)
  * URL-file reading (comment stripping)
  * Single-repo enforcement
  * Ledger writing shape
  * Pipeline registers on the Protocol
  * _build_task → write_harbor_task end-to-end (no Docker, no network)
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from repo2rlenv.bootstrap.spec import BootstrapResult, LanguageHint
from repo2rlenv.emitter.harbor import HarborTask, write_harbor_task
from repo2rlenv.github import PullRequestSummary
from repo2rlenv.pipelines.pr_to_env import (
    PrToEnvPipeline,
    UrlParseError,
    _leak_grep_v2,
    _pyproject_sanitize_snippet,
    parse_pr_url,
    read_urls_file,
)
from repo2rlenv.spec.input import (
    GenerationInput,
    OutputSpec,
    PipelineName,
    PipelineSpec,
    RepoSpec,
)
from repo2rlenv.spec.options import PrToEnvOptions


class TestParsePrUrl:
    def test_github_pull(self):
        assert parse_pr_url("https://github.com/huggingface/peft/pull/3083") == (
            "github.com",
            "huggingface",
            "peft",
            3083,
        )

    def test_github_pull_trailing_slash(self):
        assert parse_pr_url("https://github.com/huggingface/peft/pull/3083/") == (
            "github.com",
            "huggingface",
            "peft",
            3083,
        )

    def test_gitlab_mr(self):
        assert parse_pr_url("https://gitlab.com/foo/bar/-/merge_requests/42") == (
            "gitlab.com",
            "foo",
            "bar",
            42,
        )

    def test_http_variant(self):
        assert parse_pr_url("http://github.com/a/b/pull/1")[0] == "github.com"

    def test_rejects_issue_url(self):
        with pytest.raises(UrlParseError):
            parse_pr_url("https://github.com/huggingface/peft/issues/3083")

    def test_rejects_bare_repo(self):
        with pytest.raises(UrlParseError):
            parse_pr_url("https://github.com/huggingface/peft")

    def test_rejects_random(self):
        with pytest.raises(UrlParseError):
            parse_pr_url("not-a-url")


class TestReadUrlsFile:
    def test_reads_one_per_line(self, tmp_path: Path):
        p = tmp_path / "urls.txt"
        p.write_text(
            "https://github.com/huggingface/peft/pull/1\n"
            "https://github.com/huggingface/peft/pull/2\n"
        )
        assert read_urls_file(p) == [
            "https://github.com/huggingface/peft/pull/1",
            "https://github.com/huggingface/peft/pull/2",
        ]

    def test_strips_comments_and_blanks(self, tmp_path: Path):
        p = tmp_path / "urls.txt"
        p.write_text(
            "# header\n"
            "\n"
            "https://github.com/huggingface/peft/pull/1  # inline\n"
            "   \n"
            "https://github.com/huggingface/peft/pull/2\n"
        )
        assert read_urls_file(p) == [
            "https://github.com/huggingface/peft/pull/1",
            "https://github.com/huggingface/peft/pull/2",
        ]


class TestPipelineProtocol:
    def test_has_required_class_attrs(self):
        assert hasattr(PrToEnvPipeline, "name")
        assert hasattr(PrToEnvPipeline, "requires_bootstrap")
        assert PrToEnvPipeline.requires_bootstrap is True
        # Should be marked experimental while gates are landing.
        assert getattr(PrToEnvPipeline, "experimental", False) is True

    def test_is_registered(self):
        from repo2rlenv.pipelines import PIPELINES

        assert "pr_to_env" in PIPELINES


class TestLeakGrepV2:
    def test_strips_short_sha(self):
        text = "Fixed in abcdef1234 and also see deadbeef99"
        out, warns = _leak_grep_v2(text, [], [])
        assert "abcdef1234" not in out
        assert "deadbeef99" not in out
        assert warns == []

    def test_strips_pytest_nodeid(self):
        text = "Run tests/foo/test_bar.py::test_baz to verify"
        out, _ = _leak_grep_v2(text, [], [])
        assert "tests/foo/test_bar.py" not in out
        assert "test_baz" not in out

    def test_flags_basename_soft(self):
        text = "The bug is in the parser.py handling"
        out, warns = _leak_grep_v2(text, ["src/mod/parser.py"], [])
        # Not stripped, just flagged.
        assert "parser.py" in out
        assert any("parser.py" in w for w in warns)

    def test_flags_dirname_soft(self):
        text = "See the linalg module for context"
        out, warns = _leak_grep_v2(text, ["src/linalg/matrix.py"], [])
        assert "linalg" in out
        assert any("linalg" in w for w in warns)

    def test_ignores_short_hex_words(self):
        # "abc123" is only 6 chars — below the 8-char short-SHA threshold.
        text = "code abc123 remains untouched"
        out, _ = _leak_grep_v2(text, [], [])
        assert "abc123" in out

    def test_no_hits_returns_input(self):
        text = "This is a bug where the handler skips validation."
        out, warns = _leak_grep_v2(text, [], [])
        assert out == text
        assert warns == []


class TestPyprojectSanitize:
    def test_snippet_contains_pytest_check(self):
        snippet = _pyproject_sanitize_snippet()
        assert "[tool.pytest]" in snippet
        assert "[tool.pytest.ini_options]" in snippet
        # Must be a runnable RUN block ending PY heredoc.
        assert "RUN python" in snippet
        assert "'PY'" in snippet

    def test_regex_strips_bare_section(self):
        # Simulate the sanitize logic outside Docker.
        import re

        text = (
            "[tool.other]\nfoo = 1\n\n"
            "[tool.pytest]\naddopts = '--foo'\n\n"
            "[tool.pytest.ini_options]\ntestpaths = ['tests']\n"
        )
        cleaned = re.sub(
            r"^\[tool\.pytest\](?![\.\w]).*?(?=^\[|\Z)",
            "",
            text,
            count=1,
            flags=re.MULTILINE | re.DOTALL,
        )
        # The bare section is gone, but ini_options survives.
        assert "[tool.pytest]\naddopts" not in cleaned
        assert "[tool.pytest.ini_options]" in cleaned
        assert "[tool.other]" in cleaned


def test_ledger_shape(tmp_path: Path, monkeypatch):
    """_append_ledger writes one JSONL line per call with the expected fields."""
    # Build a minimal instance skipping __init__ (needs BootstrapResult).
    inst = PrToEnvPipeline.__new__(PrToEnvPipeline)
    inst._append_ledger(
        out_dir=tmp_path,
        slug="huggingface__peft-3083",
        pr_url="https://github.com/huggingface/peft/pull/3083",
        status="keeper",
        reward=1.0,
        f2p_count=5,
        p2p_count=7,
    )
    ledger = tmp_path / "keepers.jsonl"
    assert ledger.exists()
    entry = json.loads(ledger.read_text().strip())
    assert entry["slug"] == "huggingface__peft-3083"
    assert entry["status"] == "keeper"
    assert entry["reward"] == 1.0
    assert entry["f2p_count"] == 5
    assert entry["p2p_count"] == 7
    assert "timestamp" in entry


# ---------------------------------------------------------------------------
# _build_task — regression coverage for the "kwargs don't match HarborTask"
# class of bug (see REVIEW.md E1: the original build passed solve_cmd /
# eval_script / env_dockerfile / env_compose / provenance, none of which are
# HarborTask fields, and omitted the required org / repo2env — an unconditional
# TypeError that no test exercised).
# ---------------------------------------------------------------------------

_SRC_PATCH = """diff --git a/src/pkg/core.py b/src/pkg/core.py
--- a/src/pkg/core.py
+++ b/src/pkg/core.py
@@ -1,2 +1,3 @@
 def f(x):
-    return x
+    if x is None:
+        return 0
+    return x
"""

_TEST_PATCH = """diff --git a/tests/test_core.py b/tests/test_core.py
--- a/tests/test_core.py
+++ b/tests/test_core.py
@@ -1,2 +1,5 @@
 from pkg.core import f
+
+def test_none():
+    assert f(None) == 0
"""


def _pr() -> PullRequestSummary:
    return PullRequestSummary(
        number=3083,
        title="Fix crash on None input",
        body="Calling f(None) raises TypeError. No linked issue here.",
        state="closed",
        merged_at="2026-01-01T00:00:00Z",
        base_ref="main",
        base_sha="a" * 40,
        head_sha="b" * 40,
        is_draft=False,
        url="https://github.com/huggingface/peft/pull/3083",
        changed_files=["src/pkg/core.py", "tests/test_core.py"],
    )


def _pipeline() -> PrToEnvPipeline:
    gen_input = GenerationInput(
        repo=RepoSpec(url="huggingface/peft"),
        pipeline=PipelineSpec(name=PipelineName.PR_TO_ENV, options={}),
        output=OutputSpec(destination="./out", org="taiku", dataset_name="ds"),
    )
    bootstrap = BootstrapResult(
        image_digest="local/r2e-bootstrap-peft@sha256:" + "c" * 64,
        image_tag="local/r2e-bootstrap-peft:latest",
        language=LanguageHint.PYTHON,
        repo="huggingface/peft",
        ref="a" * 40,
        rebuild_cmds=["pip install -e ."],
        test_cmds=["python -m pytest -q"],
        smoke_passed=True,
        iterations=3,
        build_time_sec=12.0,
        llm_provider="anthropic/claude-sonnet-4-6",
    )
    options = PrToEnvOptions(url="https://github.com/huggingface/peft/pull/3083")
    return PrToEnvPipeline(gen_input, options, bootstrap=bootstrap)


@pytest.fixture
def built_task() -> HarborTask:
    """_build_task must return a real, constructible HarborTask.

    This alone is the regression guard for E1 — the old code raised
    TypeError before returning anything.
    """
    return _pipeline()._build_task(
        pr=_pr(),
        patch=_SRC_PATCH,
        test_patch=_TEST_PATCH,
        fail_to_pass=["tests/test_core.py::test_none"],
        pass_to_pass=["tests/test_core.py::test_identity"],
        validation_status="validated",
    )


class TestBuildTask:
    def test_returns_harbor_task_with_required_fields(self, built_task: HarborTask):
        assert isinstance(built_task, HarborTask)
        # Directory-safe slug, NOT "org/slug" — write_harbor_task appends
        # task.name to dest_dir and prefixes org itself in task.toml.
        assert built_task.name == "huggingface__peft-3083"
        assert "/" not in built_task.name
        assert built_task.org == "taiku"
        assert built_task.description == "Fix crash on None input"
        assert built_task.oracle_diff == _SRC_PATCH

    def test_only_declared_harbor_fields_are_set(self):
        """Guards against re-introducing invented kwargs (solve_cmd, provenance, ...)."""
        declared = set(HarborTask.__dataclass_fields__)
        for gone in ("solve_cmd", "eval_script", "env_dockerfile", "env_compose", "provenance"):
            assert gone not in declared

    def test_dropped_kwargs_reach_their_real_fields(self, built_task: HarborTask):
        # env_dockerfile → environment_dockerfile
        assert built_task.environment_dockerfile is not None
        assert "FROM local/r2e-bootstrap-peft" in built_task.environment_dockerfile
        assert "[tool.pytest.ini_options]" in built_task.environment_dockerfile  # gate #5
        assert "iptables" in built_task.environment_dockerfile  # egress firewall v2
        # eval_script → test_script
        assert built_task.test_script is not None
        assert "verifier.py" in built_task.test_script
        # env_compose → aux_files["environment/docker-compose.yaml"]
        compose = built_task.aux_files["environment/docker-compose.yaml"]
        assert "NET_ADMIN" in compose
        assert "/entrypoint-egress.sh" in compose
        # graded verifier artifacts still ship
        assert json.loads(built_task.aux_files["tests/f2p.json"]) == [
            "tests/test_core.py::test_none"
        ]
        assert json.loads(built_task.aux_files["tests/p2p.json"]) == [
            "tests/test_core.py::test_identity"
        ]
        assert "tests/verifier.py" in built_task.aux_files

    def test_provenance_lands_in_repo2env(self, built_task: HarborTask):
        r2e = built_task.repo2env
        assert r2e["pipeline"] == "pr_to_env"
        assert r2e["repo"] == "huggingface/peft"
        assert r2e["ref"] == "a" * 40
        assert r2e["reward_kinds"] == ["test_execution", "diff_similarity"]
        sub = r2e["pr_to_env"]
        assert sub["pr_url"] == "https://github.com/huggingface/peft/pull/3083"
        assert sub["base_commit"] == "a" * 40
        assert sub["validation_status"] == "validated"
        assert sub["reward_mode"] == "graded"
        cal = r2e["reward_calibration"]
        assert cal["f2p_count"] == 1
        # 1 F2P < min_f2p(3) → low_signal
        assert cal["calibration"] == "low_signal"

    def test_instruction_is_built_and_leak_stripped(self, built_task: HarborTask):
        assert "Fix crash on None input" in built_task.instruction
        # _build_instruction's PR-body path must have run (no TypeError from a
        # mismatched signature).
        assert "## Task" in built_task.instruction

    def test_emits_a_valid_harbor_tree(self, built_task: HarborTask, tmp_path: Path):
        task_path = write_harbor_task(built_task, tmp_path)
        assert task_path == tmp_path / "huggingface__peft-3083"

        for rel in (
            "task.toml",
            "instruction.md",
            "solution/patch.diff",
            "solution/solve.sh",
            "environment/Dockerfile",
            "environment/docker-compose.yaml",
            "tests/test.sh",
            "tests/verifier.py",
            "tests/f2p.json",
            "tests/p2p.json",
        ):
            assert (task_path / rel).is_file(), f"missing {rel}"

        payload = tomllib.loads((task_path / "task.toml").read_text())
        # Harbor requires exactly <org>/<name>.
        assert payload["task"]["name"] == "taiku/huggingface__peft-3083"
        assert payload["task"]["name"].count("/") == 1
        r2e = payload["metadata"]["repo2env"]
        assert r2e["pipeline"] == "pr_to_env"
        assert r2e["reward_kinds"] == ["test_execution", "diff_similarity"]
        assert r2e["reproducibility"]["mode"] == "local_only"
        assert "content_hash" in r2e

        # solve.sh is the home of the old `solve_cmd` kwarg.
        solve = (task_path / "solution" / "solve.sh").read_text()
        assert "git apply" in solve
        assert "patch.diff" in solve

        assert (task_path / "solution" / "patch.diff").read_text() == _SRC_PATCH


# ---------------------------------------------------------------------------
# run() — the main path landed in da423fa with zero coverage. These drive it
# end-to-end with the provider + auth stubbed and validation/oracle-gate off,
# so no Docker and no network are needed. Would have caught the
# `provider.fetch_pr` AttributeError (no provider exposed that name).
# ---------------------------------------------------------------------------


class _FakeProvider:
    """Stands in for the github/gitlab module `provider_for` returns."""

    def __init__(self, pr: PullRequestSummary, diff: str):
        self._pr = pr
        self._diff = diff
        self.fetch_pr_calls: list[tuple] = []

    def fetch_pr(self, owner, name, number, *, token=None) -> PullRequestSummary:
        self.fetch_pr_calls.append((owner, name, number, token))
        return self._pr

    def fetch_pr_diff(self, owner, name, number, *, token=None) -> str:
        return self._diff

    def fetch_issue(self, owner, name, number, *, token=None):
        return None


def test_real_providers_satisfy_the_run_loop_calls():
    """run() calls provider.<fn>(owner, name, number, token=...) — assert the
    real modules actually have those names with a compatible signature.

    This is the guard for the original AttributeError: `fetch_pr` existed on
    neither github.py nor gitlab.py, so run() died on the first URL.
    """
    import inspect

    from repo2rlenv import github, gitlab

    for mod in (github, gitlab):
        for fn_name in ("fetch_pr", "fetch_pr_diff"):
            fn = getattr(mod, fn_name, None)
            assert callable(fn), f"{mod.__name__}.{fn_name} is missing"
            sig = inspect.signature(fn)
            sig.bind("owner", "name", 1, token="tok")  # raises if incompatible


@pytest.fixture
def run_env(monkeypatch):
    """A pipeline whose run() needs neither Docker nor network."""
    from repo2rlenv.pipelines import pr_to_env as mod

    provider = _FakeProvider(_pr(), _SRC_PATCH + _TEST_PATCH)
    monkeypatch.setattr(mod, "provider_for", lambda repo: provider)
    monkeypatch.setattr(mod, "resolve_repo_token", lambda repo, auth: "tok")

    pipeline = _pipeline()
    # skip_validation → no sandbox; hard_drop_low_signal stays False so the
    # F2P floor only annotates. oracle_gate off → no harbor subprocess.
    pipeline.options.skip_validation = True
    pipeline.options.oracle_gate = False
    return pipeline, provider


class TestRun:
    def test_run_emits_a_task(self, run_env, tmp_path: Path):
        pipeline, provider = run_env
        result = pipeline.run(tmp_path)

        assert result.candidates == 1
        assert result.emitted == 1
        assert result.out_dir == tmp_path
        # The AttributeError regression: fetch_pr must exist AND be called.
        assert provider.fetch_pr_calls == [("huggingface", "peft", 3083, "tok")]

        task_dir = tmp_path / "huggingface__peft-3083"
        assert (task_dir / "task.toml").is_file()
        assert (task_dir / "tests" / "test.sh").is_file()
        assert (task_dir / "environment" / "Dockerfile").is_file()
        # Emitted at the dataset root, not nested one level deep.
        assert not (task_dir / "huggingface").exists()

    def test_run_records_validation_skipped(self, run_env, tmp_path: Path):
        pipeline, _ = run_env
        pipeline.run(tmp_path)
        payload = tomllib.loads((tmp_path / "huggingface__peft-3083" / "task.toml").read_text())
        assert payload["metadata"]["repo2env"]["pr_to_env"]["validation_status"] == "skipped"

    def test_run_reports_f2p_floor_without_dropping(self, run_env, tmp_path: Path):
        """skip_validation → 0 F2P → below the floor, but hard_drop is off."""
        pipeline, _ = run_env
        result = pipeline.run(tmp_path)
        assert result.skip_reasons.get("f2p_below_floor") == 1
        assert result.emitted == 1  # counted but not dropped

    def test_run_hard_drops_below_floor(self, run_env, tmp_path: Path):
        pipeline, _ = run_env
        pipeline.options.hard_drop_low_signal = True
        result = pipeline.run(tmp_path)
        assert result.emitted == 0
        assert result.skip_reasons["f2p_below_floor"] == 1
        assert not (tmp_path / "huggingface__peft-3083").exists()

    def test_run_skips_non_bug_pr(self, run_env, tmp_path: Path):
        pipeline, provider = run_env
        provider._pr.title = "Revert 'something'"
        result = pipeline.run(tmp_path)
        assert result.emitted == 0
        assert result.skip_reasons["non_bug_pr"] == 1

    def test_run_skips_pr_with_no_test_patch(self, run_env, tmp_path: Path):
        pipeline, provider = run_env
        provider._diff = _SRC_PATCH
        result = pipeline.run(tmp_path)
        assert result.emitted == 0
        assert result.skip_reasons["no_test_patch"] == 1

    def test_run_records_provider_failure(self, run_env, tmp_path: Path):
        from repo2rlenv.github import GitHubError

        pipeline, provider = run_env

        def boom(owner, name, number, *, token=None):
            raise GitHubError("gh 'pr view' failed")

        provider.fetch_pr = boom
        result = pipeline.run(tmp_path)
        assert result.emitted == 0
        assert result.skip_reasons["pr_fetch_failed"] == 1

    def test_run_strict_reraises_provider_failure(self, run_env, tmp_path: Path):
        from repo2rlenv.github import GitHubError

        pipeline, provider = run_env
        pipeline.options.strict = True

        def boom(owner, name, number, *, token=None):
            raise GitHubError("gh 'pr view' failed")

        provider.fetch_pr = boom
        with pytest.raises(GitHubError):
            pipeline.run(tmp_path)

    def test_run_rejects_cross_repo_urls(self, run_env, tmp_path: Path):
        pipeline, _ = run_env
        urls = tmp_path / "urls.txt"
        urls.write_text(
            "https://github.com/huggingface/peft/pull/1\nhttps://github.com/pallets/click/pull/2\n"
        )
        pipeline.options.url = None
        pipeline.options.urls_file = urls
        with pytest.raises(ValueError, match="same host\\+repo"):
            pipeline.run(tmp_path)

    def test_run_rejects_url_repo_mismatch(self, run_env, tmp_path: Path):
        pipeline, _ = run_env
        pipeline.options.url = "https://github.com/pallets/click/pull/2"
        with pytest.raises(ValueError, match="URLs point at"):
            pipeline.run(tmp_path)


class TestStartValidationSandbox:
    def test_uses_docker_sandbox_start_classmethod(self, monkeypatch):
        """Regression: it used to call DockerSandbox(image=..., language=...),
        which isn't the constructor signature (container_id, repo_mount, platform)."""
        from repo2rlenv.bootstrap import docker as docker_mod

        seen = {}

        class FakeSandbox:
            @classmethod
            def start(cls, base_image, repo_dir, *, platform="linux/amd64", **kw):
                seen.update(base_image=base_image, repo_dir=repo_dir, platform=platform)
                return cls()

        monkeypatch.setattr(docker_mod, "DockerSandbox", FakeSandbox)
        sandbox = _pipeline()._start_validation_sandbox()
        assert isinstance(sandbox, FakeSandbox)
        assert seen["base_image"] == "local/r2e-bootstrap-peft:latest"  # tag, not digest
        assert seen["platform"] == "linux/amd64"
        assert (seen["repo_dir"] / ".keep").is_file()

    def test_sandbox_is_cleaned_up_not_stopped(self, run_env, tmp_path: Path, monkeypatch):
        """DockerSandbox exposes cleanup(); run()'s finally used to call stop()."""
        pipeline, _ = run_env
        pipeline.options.skip_validation = False

        class FakeSandbox:
            cleaned = False

            def cleanup(self):
                type(self).cleaned = True

        fake = FakeSandbox()
        monkeypatch.setattr(pipeline, "_start_validation_sandbox", lambda: fake)
        monkeypatch.setattr(
            "repo2rlenv.pipelines.pr_runtime_validate.validate_pr",
            lambda **kw: type(
                "O", (), {"fail_to_pass": [], "pass_to_pass": [], "status": "validated"}
            )(),
        )
        pipeline.run(tmp_path)
        assert FakeSandbox.cleaned is True


class TestOracleGate:
    def test_skips_when_harbor_missing(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr("shutil.which", lambda _: None)
        assert _pipeline()._run_oracle_gate(task_dir=tmp_path, timeout_sec=5) is None

    def test_reads_reward_from_jobs_dir(self, monkeypatch, tmp_path: Path):
        """harbor takes `-p <dataset>`, not `--task-dir`, and writes reward.txt."""
        task_dir = tmp_path / "some__task-1"
        task_dir.mkdir()
        (task_dir / "task.toml").write_text("")
        recorded: dict[str, list[str]] = {}

        def fake_run(argv, **kw):
            recorded["argv"] = argv
            jobs = Path(argv[argv.index("--jobs-dir") + 1])
            out = jobs / "run" / "some__task-1" / "verifier"
            out.mkdir(parents=True)
            (out / "reward.txt").write_text("1.0\n")
            return type("P", (), {"stdout": "", "stderr": "", "returncode": 0})()

        monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/harbor")
        monkeypatch.setattr("subprocess.run", fake_run)

        assert _pipeline()._run_oracle_gate(task_dir=task_dir, timeout_sec=5) == 1.0
        argv = recorded["argv"]
        assert "--task-dir" not in argv  # the flag harbor doesn't have
        assert "-p" in argv and "-a" in argv and "oracle" in argv
