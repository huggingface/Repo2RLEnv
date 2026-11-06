"""Unit tests for `pipelines._pr_diff_verifier` — the in-container reward.

This is the same module that's base64-baked into pr_diff tasks. Testing
it as ordinary Python (rather than as a string blob) catches regressions
without spinning up Docker.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from repo2rlenv.pipelines._pr_diff_verifier import (
    _judge_config_from_env,
    _normalize_changes_only,
    combine,
    file_paths,
    file_targeting,
    format_valid,
    hunk_ranges,
    llm_judge,
    region_overlap,
    similarity,
    size_sanity,
)

# ---------------------------------------------------------------------------
# Parsing primitives
# ---------------------------------------------------------------------------


def test_file_paths_extracts_b_paths() -> None:
    diff = (
        "diff --git a/src/foo.py b/src/foo.py\n"
        "index 1..2 100644\n"
        "diff --git a/tests/x.py b/tests/x.py\n"
    )
    assert file_paths(diff) == {"src/foo.py", "tests/x.py"}


def test_file_paths_empty() -> None:
    assert file_paths("") == set()


def test_hunk_ranges_single_file() -> None:
    diff = "diff --git a/foo.py b/foo.py\n@@ -10,3 +10,5 @@\n@@ -100,1 +102,1 @@\n"
    out = hunk_ranges(diff)
    assert out == {"foo.py": [(10, 14), (102, 102)]}


def test_hunk_ranges_no_count_default_one() -> None:
    """`@@ -10 +10 @@` (no comma-count) means 1 line."""
    diff = "diff --git a/x b/x\n@@ -10 +10 @@\n"
    assert hunk_ranges(diff) == {"x": [(10, 10)]}


# ---------------------------------------------------------------------------
# format_valid
# ---------------------------------------------------------------------------


def test_format_valid_real_diff() -> None:
    assert format_valid("diff --git a/x.py b/x.py\n@@ -1 +1 @@\n-old\n+new\n") == 1.0


def test_format_valid_empty_zero() -> None:
    assert format_valid("") == 0.0


def test_format_valid_no_diff_header_zero() -> None:
    """Just `+/-` lines with no `diff --git` header isn't a real patch."""
    assert format_valid("+something\n-something_else\n") == 0.0


def test_format_valid_header_no_changes_zero() -> None:
    """Header but no +/- changes (only file markers) isn't a real patch."""
    assert format_valid("diff --git a/x b/x\n--- a/x\n+++ b/x\n") == 0.0


# ---------------------------------------------------------------------------
# file_targeting (F1)
# ---------------------------------------------------------------------------


def test_file_targeting_exact_match_one() -> None:
    diff = "diff --git a/foo.py b/foo.py\n"
    assert file_targeting(diff, diff) == 1.0


def test_file_targeting_no_overlap_zero() -> None:
    o = "diff --git a/foo.py b/foo.py\n"
    p = "diff --git a/bar.py b/bar.py\n"
    assert file_targeting(o, p) == 0.0


def test_file_targeting_partial_recall_two_of_three() -> None:
    """Oracle touches {a, b, c}; predicted touches {a, b, d, e}. F1 should
    give substantial credit (TP=2, FN=1, FP=2 → F1 = 4/(4+1+2) ≈ 0.57)."""
    o = "diff --git a/a b/a\ndiff --git a/b b/b\ndiff --git a/c b/c\n"
    p = "diff --git a/a b/a\ndiff --git a/b b/b\ndiff --git a/d b/d\ndiff --git a/e b/e\n"
    got = file_targeting(o, p)
    assert got == pytest.approx(4 / 7)


def test_file_targeting_is_not_jaccard() -> None:
    """Sanity check that we use F1, not Jaccard. Same case as above:
    Jaccard would be 2/5 = 0.4; F1 = 4/7 ≈ 0.57. Verify we get F1.
    """
    o = "diff --git a/a b/a\ndiff --git a/b b/b\ndiff --git a/c b/c\n"
    p = "diff --git a/a b/a\ndiff --git a/b b/b\ndiff --git a/d b/d\ndiff --git a/e b/e\n"
    got = file_targeting(o, p)
    assert got > 0.5  # F1 region, not Jaccard


def test_file_targeting_both_empty_one() -> None:
    assert file_targeting("", "") == 1.0


def test_file_targeting_predicted_empty_zero() -> None:
    o = "diff --git a/foo.py b/foo.py\n"
    assert file_targeting(o, "") == 0.0


# ---------------------------------------------------------------------------
# size_sanity
# ---------------------------------------------------------------------------


def test_size_sanity_equal_sizes_one() -> None:
    diff = "diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new\n"
    assert size_sanity(diff, diff) == 1.0


def test_size_sanity_rampage_low() -> None:
    """Predicted is 10x oracle → score ~0.1."""
    o = "diff --git a/x b/x\n+a\n-a\n"  # 2 lines
    p_lines = "\n".join(["+a"] * 20)  # 20 lines
    p = f"diff --git a/x b/x\n{p_lines}\n"
    s = size_sanity(o, p)
    assert s == pytest.approx(2 / 20)


def test_size_sanity_predicted_empty_zero() -> None:
    o = "diff --git a/x b/x\n+a\n-a\n"
    assert size_sanity(o, "") == 0.0


def test_size_sanity_both_empty_one() -> None:
    assert size_sanity("", "") == 1.0


# ---------------------------------------------------------------------------
# region_overlap
# ---------------------------------------------------------------------------


def test_region_overlap_exact_match_one() -> None:
    diff = "diff --git a/x b/x\n@@ -10,3 +10,5 @@\n"
    assert region_overlap(diff, diff) == 1.0


def test_region_overlap_wrong_file_zero() -> None:
    o = "diff --git a/foo b/foo\n@@ -10,3 +10,5 @@\n"
    p = "diff --git a/bar b/bar\n@@ -10,3 +10,5 @@\n"
    assert region_overlap(o, p) == 0.0


def test_region_overlap_off_by_three_lines_within_slack() -> None:
    """Slack=5 lines: a hunk 3 lines off should still match."""
    o = "diff --git a/x b/x\n@@ -10,3 +10,3 @@\n"  # lines 10-12
    p = "diff --git a/x b/x\n@@ -15,3 +15,3 @@\n"  # lines 15-17, 3-line gap
    assert region_overlap(o, p) == 1.0


def test_region_overlap_off_by_far_no_match() -> None:
    """Slack=5: 50 lines off → no match."""
    o = "diff --git a/x b/x\n@@ -10,1 +10,1 @@\n"
    p = "diff --git a/x b/x\n@@ -100,1 +100,1 @@\n"
    assert region_overlap(o, p) == 0.0


def test_region_overlap_one_of_two_hunks_matched() -> None:
    o = "diff --git a/x b/x\n@@ -10,1 +10,1 @@\n@@ -50,1 +50,1 @@\n"
    p = "diff --git a/x b/x\n@@ -10,1 +10,1 @@\n"
    assert region_overlap(o, p) == 0.5


# ---------------------------------------------------------------------------
# similarity (changes-only)
# ---------------------------------------------------------------------------


def test_similarity_identical_one() -> None:
    diff = "diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new\n"
    assert similarity(diff, diff) == 1.0


def test_similarity_predicted_empty_zero() -> None:
    o = "diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new\n"
    assert similarity(o, "") == 0.0


def test_similarity_ignores_context_lines() -> None:
    """Context lines (no +/- prefix) must NOT contribute to similarity.

    Two diffs with the SAME +/- changes but different context should be
    scored as identical.
    """
    o = "diff --git a/x b/x\n@@ -1,3 +1,3 @@\n ctx_a\n-old\n+new\n ctx_b\n"
    p = "diff --git a/x b/x\n@@ -100,3 +100,3 @@\n different_ctx_1\n-old\n+new\n different_ctx_2\n"
    assert similarity(o, p) == 1.0


def test_normalize_changes_only_drops_metadata_and_context() -> None:
    raw = (
        "diff --git a/x b/x\n"
        "index 1..2 100644\n"
        "--- a/x\n"
        "+++ b/x\n"
        "@@ -1,3 +1,3 @@\n"
        " context_line\n"
        "-old_line\n"
        "+new_line\n"
        " more_context\n"
    )
    out = _normalize_changes_only(raw)
    assert out == ["--- a/x", "+++ b/x", "-old_line", "+new_line"]


# ---------------------------------------------------------------------------
# llm_judge (network calls fully mocked)
# ---------------------------------------------------------------------------


def test_llm_judge_no_api_key_returns_none() -> None:
    score, status = llm_judge(instruction="i", oracle="o", predicted="p", api_key="")
    assert score is None
    assert status == "no_api_key"


def test_llm_judge_empty_predicted_returns_zero() -> None:
    score, status = llm_judge(instruction="i", oracle="o", predicted="", api_key="key")
    assert score == 0.0
    assert status == "empty_predicted"


def test_llm_judge_happy_path() -> None:
    """Mock the API to return a well-formed score."""
    fake_response = (
        b'{"content": [{"text": "{\\"score\\": 0.73, '
        b'\\"reasoning\\": \\"plausible fix in right region\\"}"}]}'
    )

    with mock.patch("urllib.request.urlopen") as m:
        m.return_value.__enter__.return_value.read.return_value = fake_response
        score, status = llm_judge(
            instruction="fix the bug",
            oracle="diff --git a/x b/x\n",
            predicted="diff --git a/x b/x\n+fix\n",
            api_key="sk-test",
        )
    assert score == pytest.approx(0.73)
    assert status == "ok"


def test_llm_judge_network_error_returns_none() -> None:
    import urllib.error

    with mock.patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        score, status = llm_judge(instruction="i", oracle="o", predicted="p", api_key="sk-test")
    assert score is None
    assert status == "network"


def test_llm_judge_parse_error_returns_none() -> None:
    """Model returns prose without a JSON object — judge should bail."""
    fake_response = b'{"content": [{"text": "Plain prose, no JSON object here at all"}]}'

    with mock.patch("urllib.request.urlopen") as m:
        m.return_value.__enter__.return_value.read.return_value = fake_response
        score, status = llm_judge(instruction="i", oracle="o", predicted="p", api_key="sk-test")
    assert score is None
    assert status == "missing_score"


@pytest.mark.parametrize("value", [1.5, -0.2, True, None, "1", float("nan"), float("inf")])
def test_llm_judge_rejects_invalid_score(value) -> None:
    fake_response = json.dumps({"content": [{"text": json.dumps({"score": value})}]}).encode()
    with mock.patch("urllib.request.urlopen") as m:
        m.return_value.__enter__.return_value.read.return_value = fake_response
        score, status = llm_judge(instruction="i", oracle="o", predicted="p", api_key="sk-test")
    assert score is None
    assert status == "invalid_score"


# ---------------------------------------------------------------------------
# llm_judge — routing: Anthropic by default, OpenAI-compatible via endpoint
# ---------------------------------------------------------------------------


_ENDPOINT = "http://127.0.0.1:8000/v1"
_OPENAI_STYLE_OK = (
    b'{"choices": [{"message": {"role": "assistant", '
    b'"content": "{\\"score\\": 0.6, \\"reasoning\\": \\"ok\\"}"}}]}'
)


def _judge_via(endpoint, response: bytes, **kw):
    """Run llm_judge with urlopen mocked; return (score, status, Request)."""
    with mock.patch("urllib.request.urlopen") as m:
        m.return_value.__enter__.return_value.read.return_value = response
        score, status = llm_judge(
            instruction="i",
            oracle="diff --git a/x b/x\n",
            predicted="diff --git a/x b/x\n+fix\n",
            api_key=kw.pop("api_key", "EMPTY"),
            endpoint=endpoint,
            **kw,
        )
        req = m.call_args.args[0]
    return score, status, req


def test_llm_judge_default_route_is_anthropic() -> None:
    fake = b'{"content": [{"text": "{\\"score\\": 0.5}"}]}'
    score, status, req = _judge_via(None, fake, api_key="sk-ant", model="claude-x")
    assert status == "ok" and score == pytest.approx(0.5)
    assert req.full_url == "https://api.anthropic.com/v1/messages"
    assert req.get_header("X-api-key") == "sk-ant"
    assert req.get_header("Authorization") is None
    assert "temperature" not in json.loads(req.data)  # default route untouched


def test_llm_judge_endpoint_uses_openai_compatible_route() -> None:
    score, status, req = _judge_via(_ENDPOINT, _OPENAI_STYLE_OK, model="Qwen/Qwen3.5-4B")
    assert status == "ok" and score == pytest.approx(0.6)
    assert req.full_url == _ENDPOINT + "/chat/completions"
    assert req.get_header("Authorization") == "Bearer EMPTY"
    assert req.get_header("X-api-key") is None
    body = json.loads(req.data)
    assert body["model"] == "Qwen/Qwen3.5-4B"
    assert body["messages"][0]["role"] == "user"
    assert body["temperature"] == 0  # greedy: small local judges are noisy otherwise


def test_llm_judge_endpoint_trailing_slash_and_explicit_key() -> None:
    _, status, req = _judge_via(_ENDPOINT + "/", _OPENAI_STYLE_OK, api_key="srv", model="m")
    assert status == "ok"
    assert req.full_url == _ENDPOINT + "/chat/completions"
    assert req.get_header("Authorization") == "Bearer srv"


def test_llm_judge_endpoint_requires_model() -> None:
    score, status = llm_judge(
        instruction="i", oracle="o", predicted="p", api_key="EMPTY", model="", endpoint=_ENDPOINT
    )
    assert score is None
    assert status == "no_judge_model"


def test_llm_judge_endpoint_anthropic_shaped_reply_is_parse_error() -> None:
    """A server answering in Anthropic's shape on the OpenAI route is a parse failure, not a crash."""
    anthropic_shaped = b'{"content": [{"text": "{\\"score\\": 0.9}"}]}'
    score, status, _ = _judge_via(_ENDPOINT, anthropic_shaped, model="m")
    assert score is None
    assert status == "parse"


def test_llm_judge_endpoint_null_content_is_missing_score() -> None:
    """A thinking-mode server can spend the whole budget on reasoning and leave
    content null. The agent's patch can provoke that, so it grades 0."""
    null_content = b'{"choices": [{"message": {"content": null, "reasoning_content": "..."}}]}'
    score, status, _ = _judge_via(_ENDPOINT, null_content, model="m")
    assert score is None
    assert status == "missing_score"


@pytest.mark.parametrize(
    "body",
    [
        # Anthropic refusal: HTTP 200, stop_reason "refusal", empty content.
        {"content": [], "stop_reason": "refusal", "stop_details": {"type": "refusal"}},
        # Streaming-classifier refusal: partial text to discard.
        {"content": [{"type": "text", "text": '{"score": 1}'}], "stop_reason": "refusal"},
        # Empty answer.
        {"content": [], "stop_reason": "end_turn"},
    ],
)
def test_llm_judge_anthropic_refusal_or_empty_is_missing_score(body) -> None:
    score, status, _ = _judge_via(None, json.dumps(body).encode())
    assert (score, status) == (None, "missing_score")


@pytest.mark.parametrize(
    "body",
    [
        {"choices": [{"message": {"content": None, "refusal": "I can't help."}}]},
        {"choices": [{"message": {"content": None}, "finish_reason": "content_filter"}]},
        {"choices": [{"message": {"content": '{"score": 1}'}, "finish_reason": "content_filter"}]},
        {"choices": [{"message": {"content": ""}}]},
    ],
)
def test_llm_judge_endpoint_refusal_or_filter_is_missing_score(body) -> None:
    score, status, _ = _judge_via(_ENDPOINT, json.dumps(body).encode(), model="m")
    assert (score, status) == (None, "missing_score")


@pytest.mark.parametrize(
    ("endpoint", "raw"),
    [
        (None, b"<html>bad gateway</html>"),
        (None, b'{"type": "error", "error": {"type": "overloaded_error"}}'),
        (None, b"[]"),
        (_ENDPOINT, b'{"error": {"message": "model not found"}}'),
        (_ENDPOINT, b'{"choices": []}'),
        (_ENDPOINT, b'{"choices": [{"message": "text"}]}'),
    ],
)
def test_llm_judge_wrong_shape_is_parse(endpoint, raw) -> None:
    score, status, _ = _judge_via(endpoint, raw, model="m")
    assert (score, status) == (None, "parse")


def test_llm_judge_endpoint_network_error() -> None:
    import urllib.error

    with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
        score, status = llm_judge(
            instruction="i",
            oracle="o",
            predicted="p",
            api_key="EMPTY",
            model="m",
            endpoint=_ENDPOINT,
        )
    assert score is None
    assert status == "network"


# ---------------------------------------------------------------------------
# _judge_config_from_env — what main() hands to llm_judge
# ---------------------------------------------------------------------------


_JUDGE_VARS = ("ANTHROPIC_API_KEY", "R2E_JUDGE_MODEL", "R2E_JUDGE_ENDPOINT", "R2E_JUDGE_API_KEY")


def _env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name in _JUDGE_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_judge_config_default_route_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    assert _judge_config_from_env() == ("", "claude-haiku-4-5-20251001", None)


def test_judge_config_default_route_with_key_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, ANTHROPIC_API_KEY=" sk-ant ", R2E_JUDGE_MODEL="claude-x")
    assert _judge_config_from_env() == ("sk-ant", "claude-x", None)


def test_judge_config_endpoint_withholds_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(
        monkeypatch,
        ANTHROPIC_API_KEY="sk-ant",
        R2E_JUDGE_ENDPOINT=_ENDPOINT,
        R2E_JUDGE_MODEL="Qwen/Qwen3.5-4B",
    )
    api_key, model, endpoint = _judge_config_from_env()
    assert api_key == "EMPTY"
    assert (model, endpoint) == ("Qwen/Qwen3.5-4B", _ENDPOINT)


def test_judge_config_endpoint_explicit_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, R2E_JUDGE_ENDPOINT=_ENDPOINT, R2E_JUDGE_MODEL="m", R2E_JUDGE_API_KEY="srv")
    assert _judge_config_from_env() == ("srv", "m", _ENDPOINT)


def test_judge_config_endpoint_without_model_has_no_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, R2E_JUDGE_ENDPOINT=_ENDPOINT)
    api_key, model, endpoint = _judge_config_from_env()
    assert api_key == "EMPTY"
    assert model == ""  # llm_judge turns this into status "no_judge_model"
    assert endpoint == _ENDPOINT


def test_judge_config_blank_endpoint_means_default_route(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, R2E_JUDGE_ENDPOINT="  ", ANTHROPIC_API_KEY="sk-ant")
    assert _judge_config_from_env() == ("sk-ant", "claude-haiku-4-5-20251001", None)


# ---------------------------------------------------------------------------
# combine — missing judges fail closed
# ---------------------------------------------------------------------------


_WEIGHTS = {
    "format_valid": 0.05,
    "size_sanity": 0.05,
    "file_targeting": 0.10,
    "region_overlap": 0.20,
    "similarity": 0.20,
    "llm_judge": 0.40,
}


def test_combine_all_components_present() -> None:
    comps: dict[str, float | None] = {
        "format_valid": 1.0,
        "size_sanity": 1.0,
        "file_targeting": 1.0,
        "region_overlap": 1.0,
        "similarity": 1.0,
        "llm_judge": 1.0,
    }
    assert combine(comps, _WEIGHTS) == pytest.approx(1.0)


def test_combine_judge_missing_returns_zero() -> None:
    """Similarity cannot substitute for missing correctness evidence."""
    comps: dict[str, float | None] = {
        "format_valid": 1.0,
        "size_sanity": 1.0,
        "file_targeting": 1.0,
        "region_overlap": 1.0,
        "similarity": 1.0,
        "llm_judge": None,
    }
    assert combine(comps, _WEIGHTS) == 0.0


def test_combine_partial_credit() -> None:
    """A valid judge preserves the weighted score."""
    comps: dict[str, float | None] = dict.fromkeys(_WEIGHTS, 0.5)
    assert combine(comps, _WEIGHTS) == pytest.approx(0.5)


def test_combine_all_none_returns_zero() -> None:
    comps: dict[str, float | None] = dict.fromkeys(_WEIGHTS, None)
    assert combine(comps, _WEIGHTS) == 0.0


# ---------------------------------------------------------------------------
# Default weights (post pilot-v4 retune)
# ---------------------------------------------------------------------------


def test_default_weights_sum_to_one() -> None:
    """The retuned defaults must still sum to 1.0 so reward stays in [0,1]."""
    from repo2rlenv.pipelines._pr_diff_verifier import _DEFAULT_WEIGHTS

    assert sum(_DEFAULT_WEIGHTS.values()) == pytest.approx(1.0)


def test_default_weights_match_pilot_recommendations() -> None:
    """Codify the post-pilot retune so weights can't silently regress."""
    from repo2rlenv.pipelines._pr_diff_verifier import _DEFAULT_WEIGHTS

    assert _DEFAULT_WEIGHTS["format_valid"] == 0.0  # zero discriminative signal
    assert _DEFAULT_WEIGHTS["llm_judge"] >= 0.4  # most informative component
    assert (
        _DEFAULT_WEIGHTS["similarity"] < _DEFAULT_WEIGHTS["region_overlap"]
    )  # correlated → don't double-count


def _main_with_judge(tmp_path, monkeypatch, status):
    from repo2rlenv.pipelines import _pr_diff_verifier as verifier

    diff = "diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new\n"
    for name, content in (("oracle", diff), ("predicted", diff), ("instruction", "Fix x")):
        (tmp_path / name).write_text(content)
    monkeypatch.setattr(verifier, "llm_judge", lambda **kwargs: (None, status))
    monkeypatch.setattr(verifier, "_read_weights_from_env", lambda: verifier._DEFAULT_WEIGHTS)
    assert (
        verifier.main(
            [
                str(tmp_path / "oracle"),
                str(tmp_path / "predicted"),
                str(tmp_path / "instruction"),
                "--out-dir",
                str(tmp_path / "out"),
            ]
        )
        == 0
    )
    return json.loads((tmp_path / "out/reward-details.json").read_text())


@pytest.mark.parametrize("status", ["no_api_key", "no_judge_model", "network", "parse"])
def test_unavailable_judge_is_ungraded_even_for_oracle(tmp_path, monkeypatch, status):
    result = _main_with_judge(tmp_path, monkeypatch, status)
    assert result["final_reward"] is None
    assert result["evaluation_status"] == "ungraded"
    assert result["eval_trustworthy"] is False
    assert result["diagnostic_similarity"] == 1
    assert result["components"]["llm_judge"] is None
    assert result["judge_status"] == status
    # Ungraded: no reward file, so Harbor reports VERIFIER_FAILED.
    assert not (tmp_path / "out/reward.txt").exists()


@pytest.mark.parametrize("status", ["missing_score", "invalid_score"])
def test_unusable_judge_reply_is_a_graded_zero(tmp_path, monkeypatch, status):
    # The agent's patch is in the judge prompt, so it can provoke these replies:
    # dropping the rollout would let it dodge a failing episode.
    result = _main_with_judge(tmp_path, monkeypatch, status)
    assert result["final_reward"] == 0
    assert result["evaluation_status"] == "graded"
    assert result["eval_trustworthy"] is False
    assert result["judge_status"] == status
    assert (tmp_path / "out/reward.txt").read_text() == "0.000000\n"


def test_disabling_judge_never_enables_similarity_reward():
    assert combine(dict.fromkeys(_WEIGHTS, 1.0), {**_WEIGHTS, "llm_judge": 0}) == 0


@pytest.mark.parametrize("invalid", ["nan", "inf", "-1", "nope"])
def test_invalid_weight_overrides_keep_default(monkeypatch, invalid):
    from repo2rlenv.pipelines._pr_diff_verifier import _DEFAULT_WEIGHTS, _read_weights_from_env

    monkeypatch.setenv("R2E_W_JUDGE", invalid)
    assert _read_weights_from_env()["llm_judge"] == _DEFAULT_WEIGHTS["llm_judge"]


def test_large_relative_weights_do_not_overflow():
    assert combine(dict.fromkeys(_WEIGHTS, 0.5), dict.fromkeys(_WEIGHTS, 1e308)) == pytest.approx(
        0.5
    )


def test_disabled_judge_is_ungraded(tmp_path, monkeypatch):
    from repo2rlenv.pipelines import _pr_diff_verifier as verifier

    weights = {**verifier._DEFAULT_WEIGHTS, "llm_judge": 0.0}
    monkeypatch.setattr(verifier, "_read_weights_from_env", lambda: weights)
    diff = "diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new\n"
    for name, content in (("oracle", diff), ("predicted", diff), ("instruction", "Fix x")):
        (tmp_path / name).write_text(content)
    args = [str(tmp_path / n) for n in ("oracle", "predicted", "instruction")]
    assert verifier.main([*args, "--out-dir", str(tmp_path / "out")]) == 0
    result = json.loads((tmp_path / "out/reward-details.json").read_text())
    assert result["judge_status"] == "disabled"
    assert result["evaluation_status"] == "ungraded" and result["final_reward"] is None
    assert not (tmp_path / "out/reward.txt").exists()


def _run_main(tmp_path, oracle, predicted):
    from repo2rlenv.pipelines import _pr_diff_verifier as verifier

    for name, content in (("oracle", oracle), ("predicted", predicted), ("instruction", "Fix x")):
        (tmp_path / name).write_text(content)
    args = [str(tmp_path / n) for n in ("oracle", "predicted", "instruction")]
    assert verifier.main([*args, "--out-dir", str(tmp_path / "out")]) == 0
    return json.loads((tmp_path / "out/reward-details.json").read_text())


_SMALL = "diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new\n"


def test_main_removes_stale_reward_files_before_an_ungraded_result(tmp_path, monkeypatch):
    # A planted or earlier reward must not stand when this run is ungraded.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("R2E_JUDGE_ENDPOINT", raising=False)
    (tmp_path / "out").mkdir()
    (tmp_path / "out/reward.txt").write_text("1.0\n")
    (tmp_path / "out/reward.json").write_text('{"reward": 1}')
    result = _run_main(tmp_path, _SMALL, _SMALL)
    assert result["evaluation_status"] == "ungraded"
    assert not (tmp_path / "out/reward.txt").exists()
    assert not (tmp_path / "out/reward.json").exists()


def test_oversized_predicted_patch_is_a_graded_zero(tmp_path, monkeypatch):
    from repo2rlenv.pipelines import _pr_diff_verifier as verifier

    monkeypatch.setattr(verifier, "llm_judge", lambda **kwargs: pytest.fail("judge called"))
    huge = "diff --git a/x b/x\n@@ -1 +1,1001 @@\n" + "+line\n" * 1001
    result = _run_main(tmp_path, _SMALL, huge)
    assert result["final_reward"] == 0
    assert result["evaluation_status"] == "graded"
    assert result["predicted_status"] == "predicted_too_large"
    assert (tmp_path / "out/reward.txt").read_text() == "0.000000\n"


def test_predicted_patch_over_the_byte_cap_is_not_read(tmp_path, monkeypatch):
    from repo2rlenv.pipelines import _pr_diff_verifier as verifier

    monkeypatch.setattr(verifier, "_MAX_PREDICTED_BYTES", 10)
    monkeypatch.setattr(verifier, "llm_judge", lambda **kwargs: pytest.fail("judge called"))
    result = _run_main(tmp_path, _SMALL, _SMALL)
    assert result["predicted_status"] == "predicted_too_large"
    assert result["final_reward"] == 0


def test_size_cap_scales_with_the_oracle():
    from repo2rlenv.pipelines._pr_diff_verifier import predicted_too_large

    oracle = "diff --git a/x b/x\n" + "+a\n" * 500
    assert not predicted_too_large(oracle, "diff --git a/x b/x\n" + "+b\n" * 5000)
    assert predicted_too_large(oracle, "diff --git a/x b/x\n" + "+b\n" * 5001)
    assert not predicted_too_large(_SMALL, "diff --git a/x b/x\n" + "+b\n" * 1000)


def test_similarity_stays_fast_on_repeated_lines():
    import time

    oracle = "diff --git a/x b/x\n" + "+x\n" * 500
    predicted = "diff --git a/x b/x\n" + "+x\n+y\n" * 2500
    start = time.monotonic()
    value = similarity(oracle, predicted)
    assert 0 <= value <= 1
    # Exact matching takes ~35 s on this input; the fallback is near-instant.
    assert time.monotonic() - start < 5
